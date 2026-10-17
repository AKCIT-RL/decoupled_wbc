"""RealSense camera sensor for ComposedCamera.

``composed_camera.py:246`` imports this module for ``camera_type == "realsense"``
but upstream never shipped it -- only ``oak.py`` exists. This fills that gap,
mirroring ``OAKSensor`` field for field so ``ComposedCameraSensor`` cannot tell
the two apart.

Contract (see ``oak.py:119-194``)::

    read() -> {"timestamps": {mount_position: wall_clock_seconds},
               "images":     {mount_position: HxWx3 uint8 RGB}}

Two choices worth knowing about:

* **RGB, not BGR.** ``oak.py:140`` flips with ``[..., ::-1]`` before publishing,
  so consumers expect RGB. We request ``bgr8`` from the SDK -- the format the
  robot's existing capture already uses -- and flip the same way.
* **Depth is off by default.** The RealSense on this G1 is on a USB 2.0 link;
  ``real/SONIC/realsense_server.py`` mocks depth entirely for that reason.
  Colour at 640x480x30 fits, colour plus depth does not.
"""

import time
from typing import Any, Dict, Optional, Tuple

import gymnasium as gym
import numpy as np
import pyrealsense2 as rs

from decoupled_wbc.control.base.sensor import Sensor
from decoupled_wbc.control.sensor.sensor_server import (
    CameraMountPosition,
    ImageMessageSchema,
    SensorServer,
)

FRAME_TIMEOUT_MS = 1000


class RealSenseConfig:
    """Configuration for the RealSense camera."""

    color_image_dim: Tuple[int, int] = (640, 480)  # (W, H), matches OAKConfig's order
    fps: int = 30
    enable_color: bool = True
    enable_depth: bool = False  # USB 2.0 bandwidth -- see module docstring
    mount_position: str = CameraMountPosition.EGO_VIEW.value


class RealSenseSensor(Sensor, SensorServer):
    """Sensor for Intel RealSense cameras."""

    def __init__(
        self,
        run_as_server: bool = False,
        port: int = 5555,
        config: RealSenseConfig = RealSenseConfig(),
        device_id: Optional[str] = None,
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
    ):
        self.config = config
        self.mount_position = mount_position
        self._run_as_server = run_as_server
        self._started = False

        ctx = rs.context()
        devices = ctx.query_devices()
        if len(devices) == 0:
            raise RuntimeError(f"No RealSense devices found for {mount_position}")

        serials = [d.get_info(rs.camera_info.serial_number) for d in devices]
        print(f"RealSense devices: {serials}")

        rs_config = rs.config()
        if device_id is not None:
            if device_id not in serials:
                raise ValueError(f"RealSense {device_id} not found. Present: {serials}")
            rs_config.enable_device(device_id)

        width, height = self.config.color_image_dim
        if self.config.enable_color:
            rs_config.enable_stream(rs.stream.color, width, height, rs.format.bgr8, self.config.fps)
        if self.config.enable_depth:
            rs_config.enable_stream(rs.stream.depth, width, height, rs.format.z16, self.config.fps)

        self.pipeline = rs.pipeline()
        profile = self.pipeline.start(rs_config)
        self._started = True

        # Stamp frames on the host clock so timestamps are comparable with the
        # control data they will be joined against. Without this the device
        # clock leaks into the dataset and image/action alignment drifts.
        for sensor in profile.get_device().query_sensors():
            if sensor.supports(rs.option.global_time_enabled):
                try:
                    sensor.set_option(rs.option.global_time_enabled, 1)
                except RuntimeError as exc:  # not fatal, just less precise
                    print(f"[WARN] global_time unavailable on {mount_position}: {exc}")

        active = profile.get_device().get_info(rs.camera_info.serial_number)
        print(f"Connected to RealSense {active} as {mount_position}")

        if self._run_as_server:
            self.start_server(port)

    def read(self) -> Optional[Dict[str, Any]]:
        """Read one frame set. Returns None on timeout rather than blocking."""
        try:
            frames = self.pipeline.wait_for_frames(FRAME_TIMEOUT_MS)
        except RuntimeError as exc:
            print(f"[ERROR] RealSense timeout on {self.mount_position}: {exc}")
            return None

        timestamps: Dict[str, float] = {}
        images: Dict[str, np.ndarray] = {}

        if self.config.enable_color:
            color_frame = frames.get_color_frame()
            if not color_frame:
                print(f"[ERROR] No colour frame from {self.mount_position}")
                return None
            # BGR -> RGB, the same flip OAKSensor applies before publishing.
            images[self.mount_position] = np.asanyarray(color_frame.get_data())[..., ::-1]
            timestamps[self.mount_position] = self._wall_clock(color_frame)

        if self.config.enable_depth:
            depth_frame = frames.get_depth_frame()
            if depth_frame:
                key = f"{self.mount_position}_depth"
                images[key] = np.asanyarray(depth_frame.get_data())
                timestamps[key] = self._wall_clock(depth_frame)

        return {"timestamps": timestamps, "images": images}

    @staticmethod
    def _wall_clock(frame) -> float:
        """Capture time in seconds on the host clock.

        With global time enabled the SDK already reports host milliseconds; if
        the backend does not support it, fall back to arrival time, which costs
        a little accuracy rather than returning a device-clock value that would
        be silently meaningless.
        """
        if frame.get_frame_timestamp_domain() == rs.timestamp_domain.global_time:
            return frame.get_timestamp() / 1000.0
        return time.time()

    def serialize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Serialize data using ImageMessageSchema."""
        return ImageMessageSchema(
            timestamps=data["timestamps"], images=data["images"]
        ).serialize()

    def observation_space(self) -> gym.Space:
        width, height = self.config.color_image_dim
        spaces = {}
        if self.config.enable_color:
            spaces["color_image"] = gym.spaces.Box(
                low=0, high=255, shape=(height, width, 3), dtype=np.uint8
            )
        if self.config.enable_depth:
            spaces["depth_image"] = gym.spaces.Box(
                low=0, high=65535, shape=(height, width), dtype=np.uint16
            )
        return gym.spaces.Dict(spaces)

    def close(self):
        if self._run_as_server:
            self.stop_server()
        if getattr(self, "_started", False):
            self.pipeline.stop()
            self._started = False

    def run_server(self):
        if not self._run_as_server:
            raise ValueError("This function is only available when run_as_server is True")
        while True:
            frame = self.read()
            if frame is None:
                continue
            self.send_message({self.mount_position: self.serialize(frame)})

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    """Standalone check: print shape and latency without the composed server."""
    import tyro

    def main(
        device_id: Optional[str] = None,
        mount_position: str = CameraMountPosition.EGO_VIEW.value,
        frames: int = 60,
    ):
        cam = RealSenseSensor(device_id=device_id, mount_position=mount_position)
        try:
            for i in range(frames):
                data = cam.read()
                if data is None:
                    continue
                for key, img in data["images"].items():
                    latency_ms = (time.time() - data["timestamps"][key]) * 1000
                    if i % 10 == 0:
                        print(f"{key}: {img.shape} {img.dtype}  latency {latency_ms:.1f} ms")
        finally:
            cam.close()

    tyro.cli(main)
