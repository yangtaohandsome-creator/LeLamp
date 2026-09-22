# Motion calibration files

These files back up the current hardware calibration from the `lamppi` Raspberry Pi 5.

On 2026-09-22 the wrist names were corrected: physical motor ID 4 is
`wrist_pitch`, and ID 5 is `wrist_roll`. These JSON files and the action data
in `motion.conf` and `lelamp/recordings/` must always be restored as one set.

- `lamppi_follower.json` restores to `~/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lamppi.json`.
- `lamppi_leader.json` restores to `~/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lamppi.json`.
- `visual_response.json` was measured before the wrist mapping correction and is
  retained as historical data. Face tracking rejects it until
  `scripts/calibrate_visual_response.py` measures a new response for motor IDs
  1 (`base_yaw`) and 4 (`wrist_pitch`).

The named poses and motion timing are stored in the repository root `motion.conf`. Recorded motions such as nodding, head shaking, and shy are stored in `lelamp/recordings/`.

Stop the app before restoring servo calibration, and keep the existing file as a local rollback copy until the restored calibration has been checked on the hardware.
