# LeLamp OpenClaw Tools

Typed OpenClaw tools that forward high-level actions to the running LeLamp Python app. The plugin never opens the servo serial port or writes joint angles directly.

Configure `baseUrl` and `token` under the OpenClaw `lelamp-tools` plugin entry. Keep the token outside version control.

Available work-light tools are `lelamp_enter_work_light`, `lelamp_update_work_light`, and `lelamp_exit_work_light`. They use the app's saved high/low poses and white/warm tones; the Python app remains the only hardware coordinator.

`lelamp_play_motion` immediately executes a motion explicitly requested by the user. `lelamp_queue_expression` queues one of `happy_wiggle`, `excited`, `sad`, `shy`, `shock`, `nod`, `headshake`, or `curious` and lets the Python app start it with the first TTS audio frame.

Timer tools create and manage multiple independent in-memory timers. Durations and remaining values use seconds. `lelamp_create_timer` may include either an `on_complete` fixed high-level Tool action or a self-contained `agent_task` that is sent through the Agent again at expiry. `LampApp` coordinates both paths; the timer module remains hardware-independent.

Alarm tools manage persistent absolute-time reminders in the IANA timezone obtained by the Pi's startup IP-location lookup. Alarms support `once`, `daily`, `weekdays`, and `weekly` with an ISO `day_of_week`, and share Timer's optional fixed action or deferred Agent task behavior.
