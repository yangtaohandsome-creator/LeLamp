import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export type SearchResult = Record<string, unknown>;

export interface SearchSource {
  readonly name: string;
  search(query: string, signal?: AbortSignal): Promise<SearchResult>;
}

export class McpSearchSource implements SearchSource {
  private clientPromise?: Promise<Client>;

  constructor(
    readonly name: string,
    private readonly url: string,
    private readonly toolName: string,
    private readonly timeoutMs: number,
    private readonly authorization?: string,
  ) {}

  private async connect(): Promise<Client> {
    if (this.clientPromise) return this.clientPromise;
    this.clientPromise = (async () => {
      const client = new Client({ name: "lelamp-web-search", version: "0.1.0" });
      const headers: Record<string, string> = {};
      if (this.authorization) headers.Authorization = `Bearer ${this.authorization}`;
      const transport = new StreamableHTTPClientTransport(new URL(this.url), {
        requestInit: { headers },
        fetch: (input, init = {}) => {
          const timeoutSignal = AbortSignal.timeout(this.timeoutMs);
          const signal = init.signal
            ? AbortSignal.any([init.signal, timeoutSignal])
            : timeoutSignal;
          return fetch(input, { ...init, signal });
        },
      });
      transport.onclose = () => {
        if (this.clientPromise) this.clientPromise = undefined;
      };
      await client.connect(transport);
      return client;
    })();
    try {
      return await this.clientPromise;
    } catch (error) {
      this.clientPromise = undefined;
      throw error;
    }
  }

  async search(query: string, signal?: AbortSignal): Promise<SearchResult> {
    try {
      const client = await this.connect();
      return await client.callTool(
        { name: this.toolName, arguments: { query } },
        undefined,
        { signal, timeout: this.timeoutMs, maxTotalTimeout: this.timeoutMs },
      );
    } catch (error) {
      const oldClient = this.clientPromise;
      this.clientPromise = undefined;
      if (oldClient) void oldClient.then((client) => client.close()).catch(() => undefined);
      throw error;
    }
  }
}

export class SearchRouter {
  constructor(
    private readonly primary: SearchSource,
    private readonly fallback?: SearchSource,
  ) {}

  async search(query: string, signal?: AbortSignal): Promise<SearchResult> {
    try {
      return { source: this.primary.name, result: await this.primary.search(query, signal) };
    } catch (primaryError) {
      if (!this.fallback) throw primaryError;
      try {
        return { source: this.fallback.name, result: await this.fallback.search(query, signal) };
      } catch (fallbackError) {
        const primaryMessage = primaryError instanceof Error ? primaryError.message : String(primaryError);
        const fallbackMessage = fallbackError instanceof Error ? fallbackError.message : String(fallbackError);
        throw new Error(`联网搜索失败：主源 ${primaryMessage}；备用源 ${fallbackMessage}`);
      }
    }
  }
}
