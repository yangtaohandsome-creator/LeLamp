/** Trim only at user-turn boundaries, retaining complete tool/result groups. */
export function trimHistory<T extends { role: string }>(messages: T[], limit: number): T[] {
  if (limit === 0) return messages; // Baseline rollback: unlimited history.
  const keep = Number.isFinite(limit) && limit >= 1 ? Math.floor(limit) : 6;
  const starts = messages.flatMap((message, index) => message.role === "user" ? [index] : []);
  return starts.length > keep ? messages.slice(starts[starts.length - keep]) : messages;
}
