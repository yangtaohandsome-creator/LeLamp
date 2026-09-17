import { Agent, type AgentEvent } from "@earendil-works/pi-agent-core";
import { contentText, type Model } from "@earendil-works/pi-ai";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-completions";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createTools, type RequestState } from "./tools.js";
import { SearchRouter } from "./search.js";
import { trimHistory } from "./history.js";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const HOST = process.env.PI_AGENT_BIND ?? "127.0.0.1";
const PORT = Number(process.env.PI_AGENT_PORT ?? "18792");
const TOKEN = process.env.PI_AGENT_TOKEN ?? "";
const ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const MAX_SESSIONS = Number(process.env.PI_AGENT_MAX_SESSIONS ?? "32");
const SESSION_TTL_MS = Number(process.env.PI_AGENT_SESSION_TTL_SECONDS ?? "1800") * 1000;

type MetricsState = RequestState & {
  startedAt: number;
  firstTokenMs?: number;
  modelCalls: number;
  modelCallDetails: Array<{
    call: number;
    first_token_ms: number | null;
    duration_ms: number;
  }>;
  activeModelStartedAt?: number;
  activeModelFirstTokenAt?: number;
};
type Session = {
  agent: Agent;
  request: MetricsState;
  lastUsed: number;
  busy: boolean;
};

const search = new SearchRouter(
  process.env.PI_SEARCH_PRIMARY_URL ?? "",
  process.env.PI_SEARCH_PRIMARY_TOKEN ?? "",
  process.env.PI_SEARCH_FALLBACK_URL ?? "",
  Number(process.env.PI_SEARCH_TIMEOUT_MS ?? "6000"),
);
const sessions = new Map<string, Session>();

function loadPrompt(): string {
  const runtimePrompt = (process.env.PI_AGENT_PROMPT_FILE ?? "").trim();
  if (runtimePrompt) {
    try { return readFileSync(resolve(ROOT, runtimePrompt), "utf8").trim(); }
    catch (error) {
      console.warn(`无法读取精简提示词 ${runtimePrompt}，回退完整提示词: ${String(error)}`);
    }
  }
  return ["IDENTITY.md", "SOUL.md", "AGENTS.md"]
    .map((name) => {
      try { return `# ${name}\n${readFileSync(resolve(ROOT, name), "utf8").trim()}`; }
      catch { return ""; }
    })
    .filter(Boolean)
    .join("\n\n");
}


function normalizeBaseUrl(value: string): string {
  const base = value.replace(/\/$/, "");
  return base.endsWith("/v1") ? base : `${base}/v1`;
}

function model(): Model<"openai-completions"> {
  return {
    id: process.env.LLM_MODEL ?? "qwen3.7-flash",
    name: process.env.LLM_MODEL ?? "qwen3.7-flash",
    api: "openai-completions",
    provider: "lelamp-qwen",
    baseUrl: normalizeBaseUrl(process.env.LLM_BASE_URL ?? "http://127.0.0.1:8083"),
    reasoning: false,
    input: ["text"],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: Number(process.env.PI_AGENT_CONTEXT_WINDOW ?? "32768"),
    maxTokens: Number(process.env.PI_AGENT_MAX_TOKENS ?? "512"),
    samplingParams: { enable_thinking: false },
    compat: {
      supportsDeveloperRole: false,
      supportsReasoningEffort: false,
      maxTokensField: "max_tokens",
    },
  };
}

function pruneSessions(): void {
  const now = Date.now();
  for (const [id, session] of sessions) {
    if (!session.busy && now - session.lastUsed > SESSION_TTL_MS) sessions.delete(id);
  }
  while (sessions.size >= MAX_SESSIONS) {
    const victim = [...sessions.entries()]
      .filter(([, item]) => !item.busy)
      .sort((a, b) => a[1].lastUsed - b[1].lastUsed)[0];
    if (!victim) break;
    sessions.delete(victim[0]);
  }
}

function makeSession(): Session {
  const request: MetricsState = {
    toolCalls: [], searchCount: 0, startedAt: 0, modelCalls: 0,
    modelCallDetails: [],
  };
  const agent = new Agent({
    initialState: {
      systemPrompt: loadPrompt(), model: model(), tools: createTools(request, search),
      thinkingLevel: "off",
    },
    streamFn: (activeModel, context, options) =>
      streamSimple(activeModel as Model<"openai-completions">, context, {
        ...options,
        samplingParams: { ...options?.samplingParams, ...(activeModel.id.startsWith("deepseek")
          ? { thinking: { type: "disabled" } } : { enable_thinking: false }) },
      }),
    getApiKey: () => process.env.OPENAI_API_KEY,
    toolExecution: "sequential",
    maxRetryDelayMs: 5000,
    beforeToolCall: async ({ toolCall }) => {
      if (
        toolCall.name === "lelamp_queue_expression"
        && request.toolCalls.some((call) => call.name !== "lelamp_queue_expression")
      ) {
        return {
          block: true,
          reason: "本轮已经执行实体或查询工具，禁止再追加自主情绪动作。请直接简短回答。",
        };
      }
      return undefined;
    },
  });
  agent.subscribe((event: AgentEvent) => {
    if (event.type === "turn_start") {
      request.activeModelStartedAt = performance.now();
      request.activeModelFirstTokenAt = undefined;
    }
    if (event.type === "message_update" && request.firstTokenMs === undefined) {
      const update = event.assistantMessageEvent;
      if (update.type === "text_delta" || update.type === "toolcall_delta") {
        request.firstTokenMs = performance.now() - request.startedAt;
      }
    }
    if (event.type === "message_update") {
      const update = event.assistantMessageEvent;
      if (
        request.activeModelFirstTokenAt === undefined
        && (update.type === "text_delta" || update.type === "toolcall_delta")
      ) {
        request.activeModelFirstTokenAt = performance.now();
      }
    }
    if (event.type === "message_end" && event.message.role === "assistant") {
      const endedAt = performance.now();
      const startedAt = request.activeModelStartedAt ?? endedAt;
      request.modelCalls += 1;
      request.modelCallDetails.push({
        call: request.modelCalls,
        first_token_ms: request.activeModelFirstTokenAt === undefined
          ? null : request.activeModelFirstTokenAt - startedAt,
        duration_ms: endedAt - startedAt,
      });
      request.activeModelStartedAt = undefined;
      request.activeModelFirstTokenAt = undefined;
    }
  });
  return { agent, request, lastUsed: Date.now(), busy: false };
}

function authorized(req: IncomingMessage): boolean {
  if (!TOKEN) return false;
  const actual = Buffer.from(req.headers.authorization ?? "");
  const expected = Buffer.from(`Bearer ${TOKEN}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function json(res: ServerResponse, status: number, body: unknown): void {
  const encoded = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(encoded) });
  res.end(encoded);
}

async function readJson(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const data = Buffer.from(chunk);
    size += data.length;
    if (size > 32 * 1024) throw new Error("请求体过大");
    chunks.push(data);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
}

function finalAnswer(agent: Agent): string {
  for (let i = agent.state.messages.length - 1; i >= 0; i -= 1) {
    const message = agent.state.messages[i];
    if (message.role === "assistant") {
      const text = contentText(message.content).trim();
      if (text) return text;
    }
  }
  return "";
}

async function chat(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const body = await readJson(req);
  const sessionId = String(body.session_id ?? "");
  const text = String(body.text ?? "").trim();
  const context = String(body.context ?? "").trim();
  if (!ID_RE.test(sessionId)) return json(res, 400, { ok: false, error: "session_id 无效" });
  if (!text || text.length > 2000) return json(res, 400, { ok: false, error: "text 无效" });
  pruneSessions();
  let session = sessions.get(sessionId);
  if (!session) {
    session = makeSession();
    sessions.set(sessionId, session);
  }
  if (session.busy) return json(res, 409, { ok: false, error: "该会话正在处理上一条消息" });
  session.busy = true;
  session.lastUsed = Date.now();
  session.request.toolCalls = [];
  session.request.searchCount = 0;
  session.request.modelCalls = 0;
  session.request.modelCallDetails = [];
  session.request.firstTokenMs = undefined;
  session.request.activeModelStartedAt = undefined;
  session.request.activeModelFirstTokenAt = undefined;
  session.request.startedAt = performance.now();
  // Keep the system/tool prefix stable for DeepSeek's automatic prefix cache.
  // Dynamic location/time follows it in the user message.
  const stablePrefix = process.env.PI_AGENT_STABLE_PREFIX !== "0";
  session.agent.state.systemPrompt = stablePrefix ? loadPrompt() : `${loadPrompt()}\n\n${context}`.trim();
  const promptText = stablePrefix && context
    ? `[系统运行上下文，不是用户原话]\n${context}\n\n[用户原话]\n${text}`
    : text;
  try {
    await session.agent.prompt(promptText);
    if (session.agent.state.errorMessage) throw new Error(session.agent.state.errorMessage);
    const answer = finalAnswer(session.agent);
    session.agent.state.messages = trimHistory(
      session.agent.state.messages, Number(process.env.PI_AGENT_HISTORY_TURNS ?? "6")
    );
    const metrics = {
      total_ms: performance.now() - session.request.startedAt,
      first_token_ms: session.request.firstTokenMs ?? null,
      model_calls: session.request.modelCalls,
      model_call_details: session.request.modelCallDetails,
      tool_calls: session.request.toolCalls,
    };
    console.log(`PI_AGENT_METRICS ${JSON.stringify(metrics)}`);
    json(res, 200, {
      ok: true, answer,
      metrics,
    });
  } catch (error) {
    json(res, 502, { ok: false, error: error instanceof Error ? error.message : String(error) });
  } finally {
    session.busy = false;
    session.lastUsed = Date.now();
  }
}

const server = createServer(async (req, res) => {
  try {
    if (!authorized(req)) return json(res, 401, { ok: false, error: "unauthorized" });
    if (req.method === "GET" && req.url === "/health") {
      return json(res, 200, { ok: true, service: "lelamp-pi-agent", sessions: sessions.size });
    }
    if (req.method === "POST" && req.url === "/v1/chat") return await chat(req, res);
    const match = req.url?.match(/^\/v1\/sessions\/([A-Za-z0-9._:-]{1,128})$/);
    if (req.method === "DELETE" && match) {
      const session = sessions.get(match[1]);
      if (session?.busy) return json(res, 409, { ok: false, error: "会话正在处理" });
      sessions.delete(match[1]);
      return json(res, 200, { ok: true });
    }
    json(res, 404, { ok: false, error: "not found" });
  } catch (error) {
    json(res, 500, { ok: false, error: error instanceof Error ? error.message : String(error) });
  }
});

if (!TOKEN) throw new Error("PI_AGENT_TOKEN 未配置");
server.listen(PORT, HOST, () => console.log(`LeLamp Pi Agent ready: http://${HOST}:${PORT}`));
