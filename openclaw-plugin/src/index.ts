import { Type } from "typebox";
import { defineToolPlugin } from "openclaw/plugin-sdk/tool-plugin";

const configSchema = Type.Object({
  baseUrl: Type.String({ description: "LeLamp control API origin." }),
  token: Type.String({ description: "LeLamp control API bearer token." }),
});

type PluginConfig = { baseUrl: string; token: string };

const expressionName = Type.Union([
  Type.Literal("happy_wiggle"), Type.Literal("excited"),
  Type.Literal("sad"), Type.Literal("shy"), Type.Literal("shock"),
  Type.Literal("nod"), Type.Literal("headshake"), Type.Literal("curious"),
]);

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

export default defineToolPlugin({
  id: "lelamp-tools",
  name: "LeLamp Tools",
  description: "Control the LeLamp robot through its coordinated high-level API.",
  configSchema,
  tools: (tool) => [
    tool({
      name: "lelamp_play_motion",
      label: "Play LeLamp motion",
      description: "Play one supported temporary lamp motion only because the user explicitly requested that exact action. NEVER use this for an emotion accompanying your own reply; you MUST use lelamp_queue_expression for autonomous expression.",
      parameters: Type.Object({
        name: expressionName,
      }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("play_motion", params, config, context.signal),
    }),
    tool({
      name: "lelamp_queue_expression",
      label: "Queue LeLamp expression",
      description: "Queue at most one expressive motion to begin with the spoken reply. You MUST use it when the reply has a clear attitude or emotion: nod=agreement, headshake=disagreement, happy_wiggle=happy, excited=strong excitement, sad=regret or empathy, shy=shy or receiving praise, shock=genuine surprise, curious=curious/confused. Neutral factual replies and clarification requests should use no expression.",
      parameters: Type.Object({ name: expressionName }, { additionalProperties: false }),
      execute: (params, config, context) =>
        callLamp("queue_expression", params, config, context.signal),
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
      name: "lelamp_get_robot_state",
      label: "Get LeLamp state",
      description: "Read the coordinated high-level state of the LeLamp robot.",
      parameters: Type.Object({}, { additionalProperties: false }),
      execute: (_params, config, context) =>
        callLamp("get_robot_state", {}, config, context.signal),
    }),
  ],
});
