import { expect, it } from "vitest";
import { trimHistory } from "./history.js";

it("retains six full turns including their tool call/result pairs", () => {
  const messages = Array.from({ length: 8 }, (_, turn) => [
    { role: "user", turn }, { role: "assistant", turn },
    { role: "toolResult", turn }, { role: "assistant", turn },
  ]).flat();
  expect(trimHistory(messages, 6)).toEqual(messages.slice(8));
  expect(trimHistory(messages, 0)).toBe(messages);
  expect(trimHistory(messages, NaN)).toEqual(messages.slice(8));
});
