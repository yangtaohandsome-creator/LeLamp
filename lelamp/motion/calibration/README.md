# Motion calibration files

These files back up the current hardware calibration from the `lamppi` Raspberry Pi 5.

- `lamppi_follower.json` restores to `~/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/lamppi.json`.
- `lamppi_leader.json` restores to `~/.cache/huggingface/lerobot/calibration/teleoperators/lelamp_leader/lamppi.json`.
- `visual_response.json` is used directly from this directory for visual tracking response calibration.

The named poses and motion timing are stored in the repository root `motion.conf`. Recorded motions such as nodding, head shaking, and shy are stored in `lelamp/recordings/`.

Stop the app before restoring servo calibration, and keep the existing file as a local rollback copy until the restored calibration has been checked on the hardware.
