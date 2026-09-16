import type { AgentTool } from "@earendil-works/pi-agent-core";
import { Type, type TSchema } from "@earendil-works/pi-ai";
import { SearchRouter } from "./search.js";

export type ToolCallMetric = {
  name: string;
  arguments: Record<string, unknown>;
  duration_ms: number;
  ok: boolean;
};

export type RequestState = { toolCalls: ToolCallMetric[]; searchCount: number };

const expression = Type.Union([
  Type.Literal("happy_wiggle"), Type.Literal("excited"), Type.Literal("sad"),
  Type.Literal("shy"), Type.Literal("shock"), Type.Literal("nod"),
  Type.Literal("headshake"), Type.Literal("curious"),
]);
const scheduledAction = Type.Object({
  tool: Type.Union([
    Type.Literal("play_motion"), Type.Literal("set_light"),
    Type.Literal("enter_work_light"), Type.Literal("update_work_light"),
    Type.Literal("exit_work_light"), Type.Literal("sleep"),
    Type.Literal("stop_tracking"), Type.Literal("turn_base"),
    Type.Literal("reset_base_heading"), Type.Literal("set_base_heading"),
  ]),
  arguments: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
}, { additionalProperties: false });

function result(value: unknown) {
  return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: value };
}

function lampTool(
  name: string, label: string, description: string, parameters: TSchema,
  controlName: string, request: RequestState,
): AgentTool {
  return {
    name, label, description, parameters,
    executionMode: "sequential",
    async execute(_id, params, signal) {
      const started = performance.now();
      let ok = false;
      try {
        const base = (process.env.LELAMP_CONTROL_URL ?? "http://127.0.0.1:18790").replace(/\/$/, "");
        const token = process.env.LELAMP_CONTROL_TOKEN ?? "";
        const response = await fetch(`${base}/v1/tools/${controlName}`, {
          method: "POST", signal,
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify(params),
        });
        const body = await response.json() as Record<string, unknown>;
        if (!response.ok || body.ok === false) {
          throw new Error(String(body.message ?? `LeLamp HTTP ${response.status}`));
        }
        ok = true;
        return result(body);
      } finally {
        request.toolCalls.push({
          name, arguments: params as Record<string, unknown>,
          duration_ms: performance.now() - started, ok,
        });
      }
    },
  };
}

export const PI_TOOL_CONTRACTS = [
  "lelamp_web_search", "lelamp_play_motion", "lelamp_queue_expression",
  "lelamp_turn_base", "lelamp_reset_base_heading", "lelamp_set_base_heading",
  "lelamp_set_light", "lelamp_enter_work_light", "lelamp_update_work_light",
  "lelamp_exit_work_light", "lelamp_sleep", "lelamp_create_timer",
  "lelamp_pause_timer", "lelamp_resume_timer", "lelamp_cancel_timer",
  "lelamp_add_timer_time", "lelamp_get_timer_remaining", "lelamp_list_timers",
  "lelamp_create_alarm", "lelamp_cancel_alarm", "lelamp_get_alarm",
  "lelamp_list_alarms", "lelamp_get_robot_state",
] as const;

export function createTools(request: RequestState, search: SearchRouter): AgentTool[] {
  const lamp = (name: string, label: string, description: string, schema: TSchema, target?: string) =>
    lampTool(name, label, description, schema, target ?? name.replace(/^lelamp_/, ""), request);
  return [
    {
      name: "lelamp_web_search", label: "Search the web",
      description: "Search current web information. Use for weather, news, traffic, prices, schedules and other changing facts. Search at most four times per user turn.",
      parameters: Type.Object({ query: Type.String({ minLength: 1, maxLength: 500 }) }, { additionalProperties: false }),
      executionMode: "sequential",
      async execute(_id, params, signal) {
        const started = performance.now();
        let ok = false;
        try {
          if (request.searchCount >= 4) throw new Error("本轮联网搜索已达到 4 次上限，请根据已有结果回答");
          request.searchCount += 1;
          const body = await search.search(String((params as { query: string }).query), signal);
          ok = true;
          return result(body);
        } finally {
          request.toolCalls.push({ name: "lelamp_web_search", arguments: params as Record<string, unknown>, duration_ms: performance.now() - started, ok });
        }
      },
    },
    lamp("lelamp_play_motion", "Play motion", "用户明确命令台灯点头、摇头或做其他动作时必须调用此工具，而且只调用一次。包括带少量 ASR 错字但动作意图明确的请求。显式动作绝不能改用 lelamp_queue_expression。", Type.Object({ name: expression }, { additionalProperties: false })),
    lamp("lelamp_queue_expression", "Queue expression", "仅用于台灯对聊天内容自主表达情绪，绝不能执行用户明确要求的点头、摇头等动作，也不能在其他实体工具成功后追加。夸奖用 shy，认同用 nod，否定用 headshake，开心用 happy_wiggle，兴奋用 excited，安慰用 sad，惊讶用 shock，好奇用 curious。", Type.Object({ name: expression }, { additionalProperties: false })),
    lamp("lelamp_turn_base", "Turn base", "Turn left or right in 30-degree relative steps.", Type.Object({ direction: Type.Union([Type.Literal("left"), Type.Literal("right")]), steps: Type.Optional(Type.Integer({ minimum: 1, maximum: 2 })) }, { additionalProperties: false })),
    lamp("lelamp_reset_base_heading", "Reset heading", "Return the base to the original front heading.", Type.Object({}, { additionalProperties: false })),
    lamp("lelamp_set_base_heading", "Set absolute heading", "Set the absolute left, front, or right heading.", Type.Object({ position: Type.Union([Type.Literal("left"), Type.Literal("front"), Type.Literal("right")]) }, { additionalProperties: false })),
    lamp("lelamp_set_light", "Set light", "Set light off, office neutral-white, warm yellow, or on alias.", Type.Object({ mode: Type.Union([Type.Literal("on"), Type.Literal("off"), Type.Literal("office"), Type.Literal("warm")]), brightness: Type.Optional(Type.Number({ minimum: 0, maximum: 100 })) }, { additionalProperties: false })),
    lamp("lelamp_enter_work_light", "Enter work light", "Start persistent desk illumination; default high pose, white tone, 75 percent.", Type.Object({ pose: Type.Optional(Type.Union([Type.Literal("high"), Type.Literal("low")])), tone: Type.Optional(Type.Union([Type.Literal("white"), Type.Literal("warm")])) }, { additionalProperties: false })),
    lamp("lelamp_update_work_light", "Adjust work light", "Adjust active work-light pose, tone, or brightness by exactly 25 points.", Type.Object({ pose: Type.Optional(Type.Union([Type.Literal("high"), Type.Literal("low")])), tone: Type.Optional(Type.Union([Type.Literal("white"), Type.Literal("warm")])), brightness_step: Type.Optional(Type.Union([Type.Literal(-25), Type.Literal(25)])) }, { additionalProperties: false })),
    lamp("lelamp_exit_work_light", "Exit work light", "End work-light mode and return to normal standby.", Type.Object({}, { additionalProperties: false })),
    lamp("lelamp_sleep", "Sleep", "Request mechanical sleep for a direct farewell or sleep request, never quotation, question, or negation.", Type.Object({ reason: Type.Union([Type.Literal("farewell"), Type.Literal("user_request")]) }, { additionalProperties: false })),
    lamp("lelamp_create_timer", "Create timer", "创建计时器。普通提醒只填写 message；用户明确要求到期执行固定机器人动作才填写 on_complete；到期时才推理则只填 agent_task。条件请求必须先读取查询或状态工具的实际结果，只有条件明确为 true 才能调用；false 或 unknown 时严禁调用。", Type.Object({ duration_seconds: Type.Number({ exclusiveMinimum: 0 }), message: Type.String(), on_complete: Type.Optional(scheduledAction), agent_task: Type.Optional(Type.String({ minLength: 1, maxLength: 2000 })) }, { additionalProperties: false })),
    lamp("lelamp_pause_timer", "Pause timer", "Pause a running timer.", Type.Object({ timer_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_resume_timer", "Resume timer", "Resume a paused timer.", Type.Object({ timer_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_cancel_timer", "Cancel timer", "Cancel a timer without firing completion.", Type.Object({ timer_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_add_timer_time", "Add timer time", "Add positive seconds to a running or paused timer.", Type.Object({ timer_id: Type.String(), seconds: Type.Number({ exclusiveMinimum: 0 }) }, { additionalProperties: false })),
    lamp("lelamp_get_timer_remaining", "Get timer", "Get one timer status and remaining seconds.", Type.Object({ timer_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_list_timers", "List timers", "List timers in creation order.", Type.Object({ include_finished: Type.Optional(Type.Boolean()) }, { additionalProperties: false })),
    lamp("lelamp_create_alarm", "Create alarm", "Create an absolute-time alarm. trigger_at uses device timezone offset; recurrence is once, daily, weekdays, or weekly. Plain reminders use message only; on_complete and agent_task are mutually exclusive.", Type.Object({ trigger_at: Type.String({ minLength: 20 }), recurrence: Type.Union([Type.Literal("once"), Type.Literal("daily"), Type.Literal("weekdays"), Type.Literal("weekly")]), day_of_week: Type.Optional(Type.Integer({ minimum: 1, maximum: 7 })), message: Type.String(), on_complete: Type.Optional(scheduledAction), agent_task: Type.Optional(Type.String({ minLength: 1, maxLength: 2000 })) }, { additionalProperties: false })),
    lamp("lelamp_cancel_alarm", "Cancel alarm", "Cancel a scheduled alarm.", Type.Object({ alarm_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_get_alarm", "Get alarm", "Read an alarm and its next trigger.", Type.Object({ alarm_id: Type.String() }, { additionalProperties: false })),
    lamp("lelamp_list_alarms", "List alarms", "List scheduled alarms in creation order.", Type.Object({ include_finished: Type.Optional(Type.Boolean()) }, { additionalProperties: false })),
    lamp("lelamp_get_robot_state", "Get robot state", "Read coordinated high-level state. base_heading_position is authoritative.", Type.Object({}, { additionalProperties: false })),
  ];
}
