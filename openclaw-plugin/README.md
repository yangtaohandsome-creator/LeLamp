# LeLamp OpenClaw Tools

Typed OpenClaw tools that forward high-level actions to the running LeLamp Python app. The plugin never opens the servo serial port or writes joint angles directly.

Configure `baseUrl` and `token` under the OpenClaw `lelamp-tools` plugin entry. Keep the token outside version control.

Available work-light tools are `lelamp_enter_work_light`, `lelamp_update_work_light`, and `lelamp_exit_work_light`. They use the app's saved high/low poses and white/warm tones; the Python app remains the only hardware coordinator.

`lelamp_play_motion` immediately executes a motion explicitly requested by the user. `lelamp_queue_expression` queues one of `happy_wiggle`, `excited`, `sad`, `shy`, `shock`, `nod`, `headshake`, or `curious` and lets the Python app start it with the first TTS audio frame.
