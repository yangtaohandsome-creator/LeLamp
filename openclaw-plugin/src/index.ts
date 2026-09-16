import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";
import { McpSearchSource, SearchRouter } from "./search.js";

const configSchema = Type.Object({
  baseUrl: Type.String({ description: "LeLamp control API origin." }),
  token: Type.String({ description: "LeLamp control API bearer token." }),
  searchPrimaryUrl: Type.Optional(Type.String({ description: "Primary MCP web-search URL." })),
  searchPrimaryToken: Type.Optional(Type.String({ description: "Primary MCP bearer token." })),
  searchFallbackUrl: Type.Optional(Type.String({ description: "Fallback MCP web-search URL." })),
  searchTimeoutMs: Type.Optional(Type.Integer({ minimum: 1000, maximum: 30000, default: 6000 })),
});

type PluginConfig = {
  baseUrl: string;
  token: string;
  searchPrimaryUrl?: string;
  searchPrimaryToken?: string;
  searchFallbackUrl?: string;
  searchTimeoutMs?: number;
};

let searchRouter: SearchRouter | undefined;
let searchConfigKey = "";

function getSearchRouter(config: PluginConfig): SearchRouter {
  if (!config.searchPrimaryUrl) throw new Error("未配置 searchPrimaryUrl");
  const timeoutMs = config.searchTimeoutMs ?? 6000;
  const key = JSON.stringify([
    config.searchPrimaryUrl, config.searchPrimaryToken,
    config.searchFallbackUrl, timeoutMs,
  ]);
  if (searchRouter && searchConfigKey === key) return searchRouter;
  const primary = new McpSearchSource(
    "dashscope", config.searchPrimaryUrl, "bailian_web_search",
    timeoutMs, config.searchPrimaryToken,
  );
  const fallback = config.searchFallbackUrl
    ? new McpSearchSource("self-hosted", config.searchFallbackUrl, "search", timeoutMs)
    : undefined;
  searchRouter = new SearchRouter(primary, fallback);
  searchConfigKey = key;
  return searchRouter;
}

const expressionName = Type.Union([
  Type.Literal("happy_wiggle"), Type.Literal("excited"),
  Type.Literal("sad"), Type.Literal("shy"), Type.Literal("shock"),
  Type.Literal("nod"), Type.Literal("headshake"), Type.Literal("curious"),
]);

const scheduledAction = Type.Object({
  tool: Type.Union([
    Type.Literal("play_motion"), Type.Literal("set_light"),
    Type.Literal("enter_work_light"), Type.Literal("update_work_light"),
    Type.Literal("exit_work_light"), Type.Literal("sleep"),
    Type.Literal("stop_tracking"), Type.Literal("turn_base"),
    Type.Literal("reset_base_heading"),
    Type.Literal("set_base_heading"),
  ]),
  arguments: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
}, { additionalProperties: false });

async function callLamp(
  tool: string,
  args: Record<string, unknown>,
  config: PluginConfig,
  signal?: AbortSignal,
) {
  const response = await fetch(
    `${config.baseUrl.replace(/\/$/, "")}/v1/tools/${tool}`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(args),
      signal,
    },
  );
  const result = (await response.json()) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(String(result.message ?? `LeLamp HTTP ${response.status}`));
  }
  return result;
}

const plugin = defineToolPlugin({
  id: "lelamp-tools",
  name: "LeLamp Tools",
  description: "Control the LeLamp robot through its coordinated high-level API.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "lelamp_web_search",
      label: "Search the web",
      description: "Search current web information. Use for weather, news, traffic, prices, schedules and other facts that may have changed. The primary source is Dashscope; a self-hosted source is used only if the primary source fails.",
      parameters: Type.Object({
        query: Type.String({ minLength: 1, maxLength: 500 }),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        getSearchRouter(config).search(params.query, context.signal),
    }),
    tool({
      name: "lelamp_play_motion",
      label: "Play LeLamp motion",
      description: "用户明确命令台灯点头、摇头或做其他动作时必须调用此工具，而且只调用一次。包括带少量 ASR 错字但动作意图明确的请求。显式动作绝不能改用 lelamp_queue_expression。",
      parameters: Type.Object({
        name: expressionName,
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("play_motion", params, config, context.signal),
    }),
    tool({
      name: "lelamp_queue_expression",
      label: "Queue LeLamp expression",
      description: "仅用于台灯对聊天内容自主表达情绪，绝不能执行用户明确要求的点头、摇头等动作，也不能在其他实体工具成功后追加。夸奖用 shy，认同用 nod，否定用 headshake，开心用 happy_wiggle，兴奋用 excited，安慰用 sad，惊讶用 shock，好奇用 curious。",
      parameters: Type.Object({ name: expressionName }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("queue_expression", params, config, context.signal),
    }),
    tool({
      name: "lelamp_turn_base",
      label: "Turn LeLamp base",
      description: "Turn the whole lamp left or right in 30-degree steps. The new heading becomes the neutral heading for later poses and motions. Use only when the user explicitly requests a turn.",
      parameters: Type.Object({
        direction: Type.Union([Type.Literal("left"), Type.Literal("right")]),
        steps: Type.Optional(Type.Integer({ minimum: 1, maximum: 2 })),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("turn_base", params, config, context.signal),
    }),
    tool({
      name: "lelamp_reset_base_heading",
      label: "Reset LeLamp base heading",
      description: "Return the lamp base to its original front heading and use it as the neutral heading again.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: (_params, config, context) =>
        callLamp("reset_base_heading", {}, config, context.signal),
    }),
    tool({
      name: "lelamp_set_base_heading",
      label: "Set LeLamp absolute base heading",
      description: "Set an absolute base heading. Use left for 最左边, front for 正前方/回正, and right for 最右边. Do not calculate relative steps for these absolute requests.",
      parameters: Type.Object({
        position: Type.Union([
          Type.Literal("left"), Type.Literal("front"), Type.Literal("right"),
        ]),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("set_base_heading", params, config, context.signal),
    }),
    tool({
      name: "lelamp_set_light",
      label: "Set LeLamp light",
      description: "Set the lamp light to off, office neutral-white, or warm yellow. Use on as an alias for office.",
      parameters: Type.Object({
        mode: Type.Union([
          Type.Literal("on"), Type.Literal("off"),
          Type.Literal("office"), Type.Literal("warm"),
        ]),
        brightness: Type.Optional(Type.Number({ minimum: 0, maximum: 100 })),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("set_light", params, config, context.signal),
    }),
    tool({
      name: "lelamp_enter_work_light",
      label: "Enter LeLamp work light",
      description: "Start persistent desk illumination. Defaults to high pose, neutral warm-white tone, and 75% brightness. Use explicit pose or tone only when the user asks for it.",
      parameters: Type.Object({
        pose: Type.Optional(Type.Union([Type.Literal("high"), Type.Literal("low")])),
        tone: Type.Optional(Type.Union([Type.Literal("white"), Type.Literal("warm")])),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("enter_work_light", params, config, context.signal),
    }),
    tool({
      name: "lelamp_update_work_light",
      label: "Adjust LeLamp work light",
      description: "Adjust an active desk-light mode. Change pose, tone, or brightness by exactly 25 percentage points. Brightness stays between 50% and 100%.",
      parameters: Type.Object({
        pose: Type.Optional(Type.Union([Type.Literal("high"), Type.Literal("low")])),
        tone: Type.Optional(Type.Union([Type.Literal("white"), Type.Literal("warm")])),
        brightness_step: Type.Optional(Type.Union([Type.Literal(-25), Type.Literal(25)])),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("update_work_light", params, config, context.signal),
    }),
    tool({
      name: "lelamp_exit_work_light",
      label: "Exit LeLamp work light",
      description: "End persistent desk illumination. Fade the light down and return the lamp to its normal standby pose while keeping the application running.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: (_params, config, context) =>
        callLamp("exit_work_light", {}, config, context.signal),
    }),
    tool({
      name: "lelamp_sleep",
      label: "Put LeLamp to sleep",
      description: "Request the end of the current spoken conversation and mechanical sleep. Call this when the user directly says goodbye, says they are leaving, says the conversation is over, or asks the lamp to sleep. Do not call it for quoted speech, questions about goodbye, or negated requests such as 不要睡眠.",
      parameters: Type.Object({
        reason: Type.Union([Type.Literal("farewell"), Type.Literal("user_request")]),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("sleep", params, config, context.signal),
    }),
    tool({
      name: "lelamp_create_timer",
      label: "Create LeLamp timer",
      description: "Create an independent timer. For a plain spoken reminder, set only duration_seconds and message: NEVER invent on_complete motion or lighting. Use on_complete only for a robot action the user explicitly requested. HARD RULE for an immediate conditional request: evaluate the condition after search, and call this tool only when the condition is explicitly TRUE. If the condition is false, unknown, unsupported, or merely mentioned alongside a duration/reminder, NEVER call this tool. This remains true when reusing search evidence from an earlier turn. Never issue search and timer creation in parallel. Use agent_task only when searching or deciding must happen at expiry; make it self-contained and do not set on_complete with it.",
      parameters: Type.Object({
        duration_seconds: Type.Number({ exclusiveMinimum: 0 }),
        message: Type.String(),
        on_complete: Type.Optional(scheduledAction),
        agent_task: Type.Optional(Type.String({ minLength: 1, maxLength: 2000 })),
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("create_timer", params, config, context.signal),
    }),
    tool({
      name: "lelamp_pause_timer",
      label: "Pause LeLamp timer",
      description: "Pause one running timer by timer_id.",
      parameters: Type.Object({ timer_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("pause_timer", params, config, context.signal),
    }),
    tool({
      name: "lelamp_resume_timer",
      label: "Resume LeLamp timer",
      description: "Resume one paused timer by timer_id.",
      parameters: Type.Object({ timer_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("resume_timer", params, config, context.signal),
    }),
    tool({
      name: "lelamp_cancel_timer",
      label: "Cancel LeLamp timer",
      description: "Cancel one active timer by timer_id. Cancellation does not fire its completion reminder.",
      parameters: Type.Object({ timer_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("cancel_timer", params, config, context.signal),
    }),
    tool({
      name: "lelamp_add_timer_time",
      label: "Add time to LeLamp timer",
      description: "Add a positive number of seconds to one running or paused timer.",
      parameters: Type.Object({
        timer_id: Type.String(),
        seconds: Type.Number({ exclusiveMinimum: 0 }),
      }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("add_timer_time", params, config, context.signal),
    }),
    tool({
      name: "lelamp_get_timer_remaining",
      label: "Get LeLamp timer remaining",
      description: "Get the current state and remaining seconds of one timer.",
      parameters: Type.Object({ timer_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("get_timer_remaining", params, config, context.signal),
    }),
    tool({
      name: "lelamp_list_timers",
      label: "List LeLamp timers",
      description: "List timers in creation order. By default only running and paused timers are returned. Use this before resolving ordinal references such as the first timer.",
      parameters: Type.Object({ include_finished: Type.Optional(Type.Boolean()) }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("list_timers", params, config, context.signal),
    }),
    tool({
      name: "lelamp_create_alarm",
      label: "Create LeLamp alarm",
      description: "Create a persistent absolute-time alarm. trigger_at MUST be a future ISO 8601 timestamp with the device timezone offset supplied in context. Use once for one occurrence, daily for every day, weekdays for Monday-Friday, and weekly with ISO day_of_week (1=Monday ... 7=Sunday) for a specific weekday. Plain reminders need only message; use on_complete only for an explicitly requested robot action, or agent_task when fresh reasoning/search is required at trigger time. Never set both.",
      parameters: Type.Object({
        trigger_at: Type.String({ minLength: 20 }),
        recurrence: Type.Union([
          Type.Literal("once"), Type.Literal("daily"), Type.Literal("weekdays"),
          Type.Literal("weekly"),
        ]),
        day_of_week: Type.Optional(Type.Integer({ minimum: 1, maximum: 7 })),
        message: Type.String(),
        on_complete: Type.Optional(scheduledAction),
        agent_task: Type.Optional(Type.String({ minLength: 1, maxLength: 2000 })),
      }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("create_alarm", params, config, context.signal),
    }),
    tool({
      name: "lelamp_cancel_alarm",
      label: "Cancel LeLamp alarm",
      description: "Cancel one scheduled alarm by alarm_id.",
      parameters: Type.Object({ alarm_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("cancel_alarm", params, config, context.signal),
    }),
    tool({
      name: "lelamp_get_alarm",
      label: "Get LeLamp alarm",
      description: "Read one alarm and its next trigger time.",
      parameters: Type.Object({ alarm_id: Type.String() }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("get_alarm", params, config, context.signal),
    }),
    tool({
      name: "lelamp_list_alarms",
      label: "List LeLamp alarms",
      description: "List alarms in creation order. By default returns only scheduled alarms.",
      parameters: Type.Object({ include_finished: Type.Optional(Type.Boolean()) }, { additionalProperties: false }),
      execute: (params, config, context) => callLamp("list_alarms", params, config, context.signal),
    }),
    tool({
      name: "lelamp_get_robot_state",
      label: "Get LeLamp state",
      description: "Read the coordinated high-level state of the LeLamp robot. For base orientation, use base_heading_position as the authoritative left/front/right value; do not infer direction from the signed degree number.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: (_params, config, context) =>
        callLamp("get_robot_state", {}, config, context.signal),
    }),
  ],
});

// A prompt instruction alone cannot reliably stop a model from repeatedly
// retrying weak search results. Enforce a small per-run budget at the tool
// boundary so one question cannot consume the whole voice-assistant timeout.
const baseRegister = plugin.register;
const searchRuns = new Map<string, { count: number; touchedAt: number }>();

plugin.register = (api) => {
  baseRegister(api);
  api.on(
    "before_tool_call",
    (event, context) => {
      if (
        event.toolName !== "lelamp_web_search"
      ) return;
      const runId = event.runId ?? context.runId;
      if (!runId) return;

      const now = Date.now();
      for (const [key, state] of searchRuns) {
        if (now - state.touchedAt > 10 * 60_000) searchRuns.delete(key);
      }

      const state = searchRuns.get(runId) ?? { count: 0, touchedAt: now };
      state.touchedAt = now;
      if (state.count >= 4) {
        return {
          block: true,
          blockReason:
            "本轮联网搜索已达到 4 次上限。禁止继续搜索；请立即根据已有可靠结果简短回答，无法确认时明确说明拿不到准确数据。",
        };
      }
      state.count += 1;
      searchRuns.set(runId, state);
    },
    {
      matcher: ["lelamp_web_search"],
      priority: 100,
    },
  );
};

export default plugin;
