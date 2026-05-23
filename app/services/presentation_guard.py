"""
app/services/presentation_guard.py
Fast guard for phone/photo presentation attacks.

This intentionally does not try to solve full liveness. It targets the common
case where a face photo is shown on a phone/laptop/tablet in front of the camera.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import time

import cv2
import numpy as np

from app.core.config import settings


@dataclass
class GuardSample:
    ts: float
    bbox: list[int]
    landmarks: list[list[float]]
    similarity: float
    frame_risk: float


class PresentationGuardService:
    def __init__(self):
        self.enabled = settings.PRESENTATION_GUARD_ENABLED
        self.action = settings.PRESENTATION_GUARD_ACTION
        self.threshold = settings.PRESENTATION_GUARD_RISK_THRESHOLD
        self.min_frames = max(1, settings.PRESENTATION_GUARD_MIN_FRAMES)
        self.window_seconds = max(1.0, settings.PRESENTATION_GUARD_WINDOW_SECONDS)
        self._samples: dict[str, deque[GuardSample]] = defaultdict(lambda: deque(maxlen=8))

    def reset(self, emp_code: str) -> None:
        self._samples.pop(emp_code, None)

    def check(
        self,
        frame: np.ndarray | None,
        result: dict,
        emp_code: str = "",
        now: float | None = None,
    ) -> dict:
        if not self.enabled:
            return self._result(True, False, "disabled", 0.0, {})

        bbox = result.get("bbox") or []
        if frame is None or len(bbox) != 4:
            return self._result(True, False, "no_frame", 0.0, {})

        rect_score, rect_metrics = self._device_rectangle_score(frame, bbox)
        edge_score, edge_metrics = self._straight_edge_score(frame, bbox)
        artifact_score, artifact_metrics = self._screen_artifact_score(frame, bbox)

        frame_risk = min(1.0, rect_score + edge_score + artifact_score)
        reasons = []
        if rect_score:
            reasons.append("device_rectangle")
        if edge_score:
            reasons.append("phone_edges")
        if artifact_score:
            reasons.extend(artifact_metrics.get("reasons", []))

        metrics = {
            "risk": round(frame_risk, 4),
            "rectangle_score": rect_score,
            "edge_score": edge_score,
            "artifact_score": artifact_score,
            "reasons": sorted(set(reasons)),
            **rect_metrics,
            **edge_metrics,
            **artifact_metrics,
        }

        if frame_risk >= self.threshold:
            return self._result(False, self.action == "block", "presentation_risk", frame_risk, metrics)

        temporal = self._update_temporal(emp_code, bbox, result, frame_risk, now)
        if temporal["reason"] == "collecting_frames":
            return self._result(False, False, "collecting_frames", frame_risk, metrics | temporal["metrics"])

        total_risk = min(1.0, frame_risk + temporal["risk"])
        merged_metrics = metrics | temporal["metrics"]
        merged_metrics["risk"] = round(total_risk, 4)
        merged_metrics["reasons"] = sorted(set(reasons + temporal["reasons"]))
        if total_risk >= self.threshold:
            return self._result(False, self.action == "block", "presentation_risk", total_risk, merged_metrics)
        return self._result(True, False, "clear", total_risk, merged_metrics)

    def _update_temporal(
        self,
        emp_code: str,
        bbox: list[int],
        result: dict,
        frame_risk: float,
        now: float | None,
    ) -> dict:
        if not emp_code:
            return {"reason": "ready", "risk": 0.0, "reasons": [], "metrics": {"frames": 1}}

        ts = now or time.monotonic()
        samples = self._samples[emp_code]
        samples.append(GuardSample(
            ts=ts,
            bbox=[int(v) for v in bbox],
            landmarks=result.get("landmarks") or [],
            similarity=float(result.get("similarity") or 0.0),
            frame_risk=frame_risk,
        ))
        cutoff = ts - self.window_seconds
        while samples and samples[0].ts < cutoff:
            samples.popleft()

        if len(samples) < self.min_frames:
            return {
                "reason": "collecting_frames",
                "risk": 0.0,
                "reasons": [],
                "metrics": {"frames": len(samples), "needed_frames": self.min_frames},
            }

        temporal_risk, metrics, reasons = self._temporal_risk(samples)
        return {"reason": "ready", "risk": temporal_risk, "reasons": reasons, "metrics": metrics}

    def _temporal_risk(self, samples: deque[GuardSample]) -> tuple[float, dict, list[str]]:
        bbox_motion = self._bbox_motion(samples)
        bbox_scale_motion = self._bbox_scale_motion(samples)
        geometry_motion = self._geometry_motion(samples)
        similarity_std = float(np.std([s.similarity for s in samples]))
        frame_risk_max = max(s.frame_risk for s in samples)

        risk = 0.0
        reasons = []
        if (bbox_motion >= 0.006 or bbox_scale_motion >= 0.010) and geometry_motion < 0.0045:
            risk += 0.42
            reasons.append("rigid_screen_motion")
        if bbox_motion < 0.0025 and geometry_motion < 0.0015 and similarity_std < 0.002:
            risk += 0.22
            reasons.append("static_photo")
        if frame_risk_max >= 0.25 and geometry_motion < 0.0045:
            risk += 0.18
            reasons.append("screen_artifacts_with_flat_geometry")

        metrics = {
            "frames": len(samples),
            "temporal_risk": round(min(risk, 1.0), 4),
            "bbox_motion": round(bbox_motion, 5),
            "bbox_scale_motion": round(bbox_scale_motion, 5),
            "geometry_motion": round(geometry_motion, 5),
            "similarity_std": round(similarity_std, 5),
            "frame_risk_max": round(frame_risk_max, 4),
        }
        return min(risk, 1.0), metrics, reasons

    @staticmethod
    def _bbox_motion(samples: deque[GuardSample]) -> float:
        centers = []
        for sample in samples:
            x1, y1, x2, y2 = sample.bbox
            size = max(1.0, ((x2 - x1) + (y2 - y1)) / 2)
            centers.append([(x1 + x2) / 2 / size, (y1 + y2) / 2 / size])
        if len(centers) < 2:
            return 0.0
        return float(np.mean(np.std(np.array(centers, dtype=np.float32), axis=0)))

    @staticmethod
    def _bbox_scale_motion(samples: deque[GuardSample]) -> float:
        sizes = []
        aspects = []
        for sample in samples:
            x1, y1, x2, y2 = sample.bbox
            width = max(1.0, x2 - x1)
            height = max(1.0, y2 - y1)
            sizes.append((width * height) ** 0.5)
            aspects.append(width / height)
        if len(sizes) < 2:
            return 0.0
        size_arr = np.array(sizes, dtype=np.float32)
        mean_size = max(1.0, float(np.mean(size_arr)))
        area_motion = float(np.std(size_arr) / mean_size)
        aspect_motion = float(np.std(np.array(aspects, dtype=np.float32)))
        return area_motion + aspect_motion

    @staticmethod
    def _geometry_motion(samples: deque[GuardSample]) -> float:
        normalized = []
        for sample in samples:
            if len(sample.landmarks) < 5:
                continue
            x1, y1, x2, y2 = sample.bbox
            width = max(1.0, x2 - x1)
            height = max(1.0, y2 - y1)
            points = np.array(sample.landmarks, dtype=np.float32)
            points[:, 0] = (points[:, 0] - x1) / width
            points[:, 1] = (points[:, 1] - y1) / height
            normalized.append(points.reshape(-1))
        if len(normalized) < 2:
            return 0.0
        return float(np.mean(np.std(np.array(normalized, dtype=np.float32), axis=0)))

    def _device_rectangle_score(self, frame: np.ndarray, bbox: list[int]) -> tuple[float, dict]:
        x1, y1, x2, y2 = [int(v) for v in bbox]
        face_cx = (x1 + x2) / 2
        face_cy = (y1 + y2) / 2
        face_area = max(1, (x2 - x1) * (y2 - y1))

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 45, 140)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = 0.0
        best_box = None
        for contour in contours:
            rx, ry, rw, rh = cv2.boundingRect(contour)
            rect_area = rw * rh
            if rect_area < face_area * 1.15 or rect_area > face_area * 45:
                continue
            if not (rx <= face_cx <= rx + rw and ry <= face_cy <= ry + rh):
                continue
            aspect = rw / max(1, rh)
            if not (0.35 <= aspect <= 2.8):
                continue
            if rx > x1 or ry > y1 or rx + rw < x2 or ry + rh < y2:
                continue

            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.035 * peri, True)
            edge_fill = cv2.contourArea(contour) / max(1, rect_area)
            score = 0.0
            if 4 <= len(approx) <= 10:
                score += 0.35
            if 0.03 <= edge_fill <= 0.95:
                score += 0.15
            if rect_area >= face_area * 2.0:
                score += 0.10
            if score > best:
                best = score
                best_box = [rx, ry, rw, rh]

        return min(best, 0.65), {"device_box": best_box}

    def _straight_edge_score(self, frame: np.ndarray, bbox: list[int]) -> tuple[float, dict]:
        crop, offset = self._expanded_crop(frame, bbox, scale=3.2)
        if crop is None:
            return 0.0, {"phone_edge_lines": 0}

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)
        min_len = max(35, int(min(crop.shape[:2]) * 0.28))
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=45, minLineLength=min_len, maxLineGap=12)
        if lines is None:
            return 0.0, {"phone_edge_lines": 0}

        x1, y1, x2, y2 = [int(v) for v in bbox]
        ox, oy = offset
        local_face = [x1 - ox, y1 - oy, x2 - ox, y2 - oy]
        line_count = 0
        for line in lines[:, 0]:
            lx1, ly1, lx2, ly2 = [int(v) for v in line]
            dx = abs(lx2 - lx1)
            dy = abs(ly2 - ly1)
            if dx < 1 and dy < 1:
                continue
            vertical = dy > dx * 2.5
            horizontal = dx > dy * 2.5
            if not (vertical or horizontal):
                continue
            if vertical and min(lx1, lx2) <= local_face[0] and max(ly1, ly2) >= local_face[3]:
                line_count += 1
            elif vertical and max(lx1, lx2) >= local_face[2] and max(ly1, ly2) >= local_face[3]:
                line_count += 1
            elif horizontal and min(ly1, ly2) <= local_face[1] and max(lx1, lx2) >= local_face[2]:
                line_count += 1
            elif horizontal and max(ly1, ly2) >= local_face[3] and max(lx1, lx2) >= local_face[2]:
                line_count += 1

        if line_count >= 3:
            return 0.35, {"phone_edge_lines": int(line_count)}
        if line_count >= 2:
            return 0.20, {"phone_edge_lines": int(line_count)}
        return 0.0, {"phone_edge_lines": int(line_count)}

    def _screen_artifact_score(self, frame: np.ndarray, bbox: list[int]) -> tuple[float, dict]:
        crop, _offset = self._expanded_crop(frame, bbox, scale=1.7)
        if crop is None:
            return 0.0, {"reasons": []}

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        score = 0.0
        reasons = []

        bright_ratio = float((gray > 245).mean())
        if bright_ratio > 0.035:
            score += 0.12
            reasons.append("glare")

        lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if lap_var > 900:
            score += 0.12
            reasons.append("screen_texture")

        sat_mean = float(hsv[:, :, 1].mean())
        if sat_mean > 120:
            score += 0.10
            reasons.append("oversaturated")

        return min(score, 0.30), {
            "reasons": reasons,
            "bright_ratio": round(bright_ratio, 4),
            "lap_var": round(lap_var, 2),
            "sat_mean": round(sat_mean, 2),
        }

    @staticmethod
    def _expanded_crop(frame: np.ndarray, bbox: list[int], scale: float) -> tuple[np.ndarray | None, tuple[int, int]]:
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = [int(v) for v in bbox]
        bw = max(1, x2 - x1)
        bh = max(1, y2 - y1)
        pad_x = int(bw * (scale - 1.0) / 2)
        pad_y = int(bh * (scale - 1.0) / 2)
        left = max(0, x1 - pad_x)
        top = max(0, y1 - pad_y)
        right = min(w, x2 + pad_x)
        bottom = min(h, y2 + pad_y)
        crop = frame[top:bottom, left:right]
        return (crop if crop.size else None), (left, top)

    def _result(self, is_live: bool, should_block: bool, reason: str, risk: float, metrics: dict) -> dict:
        return {
            "enabled": self.enabled,
            "action": self.action,
            "is_live": is_live,
            "should_block": should_block,
            "reason": reason,
            "risk": round(risk, 4),
            "metrics": metrics,
        }


presentation_guard_service = PresentationGuardService()
