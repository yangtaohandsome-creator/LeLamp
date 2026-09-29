import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export type SearchResult = Record<string, unknown>;

export class SearchRouter {
  private primary?: Promise<Client>;
  private fallback?: Promise<Client>;

  constructor(
    private readonly primaryUrl: string,
    private readonly primaryToken: string,
    private readonly fallbackUrl: string,
    private readonly timeoutMs: number,
  ) {}

  private connect(url: string, token: string, name: string): Promise<Client> {
    return (async () => {
      const client = new Client({ name: `lelamp-pi-${name}`, version: "0.1.0" });
      const headers: Record<string, string> = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      const transport = new StreamableHTTPClientTransport(new URL(url), {
        requestInit: { headers },
        fetch: (input, init = {}) => {
          const timeout = AbortSignal.timeout(this.timeoutMs);
          const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
          return fetch(input, { ...init, signal });
        },
      });
      await client.connect(transport);
      return client;
    })();
  }

  private async call(
    kind: "primary" | "fallback", url: string, token: string,
    tool: string, query: string, signal?: AbortSignal,
  ): Promise<SearchResult> {
    let promise = this[kind];
    if (!promise) {
      promise = this.connect(url, token, kind);
      this[kind] = promise;
    }
    try {
      const client = await promise;
      return await client.callTool(
        { name: tool, arguments: { query } }, undefined,
        { signal, timeout: this.timeoutMs, maxTotalTimeout: this.timeoutMs },
      ) as SearchResult;
    } catch (error) {
      this[kind] = undefined;
      void promise.then((client) => client.close()).catch(() => undefined);
      throw error;
    }
  }

  async search(query: string, signal?: AbortSignal): Promise<SearchResult> {
    if (!this.primaryUrl) throw new Error("未配置 PI_SEARCH_PRIMARY_URL");
    try {
      return {
        source: "dashscope",
        result: await this.call(
          "primary", this.primaryUrl, this.primaryToken,
          "bailian_web_search", query, signal,
        ),
      };
    } catch (primaryError) {
      if (!this.fallbackUrl) throw primaryError;
      try {
        return {
          source: "self-hosted",
          result: await this.call(
            "fallback", this.fallbackUrl, "", "search", query, signal,
          ),
        };
      } catch (fallbackError) {
        throw new Error(
          `联网搜索失败：主源 ${String(primaryError)}；备用源 ${String(fallbackError)}`,
        );
      }
    }
  }
}
