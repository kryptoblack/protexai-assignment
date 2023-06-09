import cv2
import json
import os
import numpy as np
from shapely.geometry import Polygon
from typing import List, Dict, Any, Optional, Set, Tuple
from dotenv import load_dotenv

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


class RuleEngine:
    """RuleEngine class acts as a base for rule implementations"""

    # sets object class to True if it is in
    # that region of interest (roi) else sets it to False
    _objects_in_rois: List[Dict[str, bool]] = list()

    # stores region of interest in the form of shapely Polygon for calculations
    _rois: List[Polygon] = list()

    # the difference between current positive frame and last positive frame
    # (current_positive_frame - last_positive_frame) should be greater than
    # maximum_allowed_diff to trigger an event notification
    #
    # Note: "positive frames" are frames at which the rule
    #       result returned positive
    #
    # Motive: this is done to avoid sending messages for the same event
    #
    _maximum_allowed_diff: int = 1
    _last_positive_frame: int = 0

    # slack client that is used for notification. if token is not present
    # __slack_client will stay None and self.should_notify will always return False
    __slack_client: Optional[WebClient] = None
    __slack_channel: str

    def __init__(self, rois: List[List[Tuple[int, int]]]) -> None:
        for roi in rois:
            self._rois.append(Polygon(roi))
            self._objects_in_rois.append(
                {"car": False, "person": False, "truck": False}
            )

        # load environment variables
        load_dotenv()
        if os.getenv("SLACK_TOKEN"):
            self.__slack_client = WebClient(token=os.environ["SLACK_TOKEN"])

        # os.environ is used instead of os.getenv to raise an exception
        #
        # if __slack_token is present then we need __slack_channel
        # to be present otherwise the program will fail at notify.
        # So this allows the program to fail during initialization instead.
        self.__slack_channel = os.environ["SLACK_CHANNEL"]

    def reset_objects(self) -> None:
        """This method is required to reset the objects in all the region of interests.

        It is done to so previous frames objects do not interfer with the new frame.
        """
        for index in range(len(self._objects_in_rois)):
            self._objects_in_rois[index] = {
                "car": False,
                "person": False,
                "truck": False,
            }

    def calculate_coords(
        self,
        left: float,
        top: float,
        width: float,
        height: float,
        frame_width: int,
        frame_height: int,
    ) -> List[Tuple[float, float]]:
        """This method is used to convert fractional values to actual values"""
        return [
            (
                # top-left
                left * frame_width,
                top * frame_height,
            ),
            (
                # top-right
                (left + width) * frame_width,
                top * frame_height,
            ),
            (
                # bottom-right
                (left + width) * frame_width,
                (top + height) * frame_height,
            ),
            (
                # bottom-left
                left * frame_width,
                (top + height) * frame_height,
            ),
        ]

    def should_notify(self, current_frame: int) -> bool:
        """This method is responsible to tell if new notification is necessary"""
        res: bool = self.__slack_client != None and self._maximum_allowed_diff < (
            current_frame - self._last_positive_frame
        )
        self._last_positive_frame = current_frame
        return res

    def notify(self, timestamp: int, rule_name: str, cam_name: str) -> None:
        if self.__slack_client == None:
            return

        # for better human readable time
        seconds = timestamp / 1e9

        seconds = seconds % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60

        readable_time = list()
        if hour > 0:
            readable_time.append(f"{int(hour)} hours ")
        if minutes > 0:
            readable_time.append(f"{int(minutes)} minutes ")
        if seconds > 0:
            readable_time.append(f"{int(seconds)} seconds ")

        time_str: str = "".join(readable_time)

        try:
            self.__slack_client.chat_postMessage(
                channel=self.__slack_channel,
                text=f"*A new event has occurred:* \n*Rule Name:* {rule_name}\n*When:* {time_str}after origin\n*Camera Name:* {cam_name}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":warning: *A new event has occurred:*",
                        },
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Rule Name:* {rule_name}\n*When:* {time_str}after origin\n*Camera Name:* {cam_name}",
                        },
                    },
                ],
            )
        except SlackApiError as e:
            assert e.response["error"]


class CAP(RuleEngine):
    """CAP stands for Car And Person. It is an Rule class derived from
    the RuleEngine.

    This rule states that a Car and a Person can never be in
    the same region of interest (roi) at the same time.

    Note: A better implementation can be achieved to make
    any RuleEngine derived class agnostic of then event loop outside.
    """

    rule_name: str = "Car and Person"
    _count: int = 0

    def __init__(self, rois: List[List[Tuple[int, int]]], *args, **kwargs) -> None:
        super().__init__(rois, *args, **kwargs)

    def execute(
        self,
        object: Polygon,
        obj_class: str,
        frame_num: int,
        cam_name: str,
        timestamp: int,
    ) -> int:
        """Responsible to execute the CAP rule"""

        for index, roi in enumerate(self._rois):
            if roi.intersects(object.centroid):
                self._objects_in_rois[index][obj_class] = True

                # rule checking
                if (
                    self._objects_in_rois[index]["car"]
                    and self._objects_in_rois[index]["person"]
                ):
                    self._count += 1
                    if self.should_notify(frame_num):
                        self.notify(
                            timestamp=timestamp,
                            cam_name=cam_name,
                            rule_name=self.rule_name,
                        )
                    return index
                break

        return -1


class Render(CAP):
    """Handles mp4 creation and also manages the event loop"""

    __colors: Dict[str, Tuple[int, int, int]] = {
        "person": (50, 168, 82),
        "car": (144, 127, 250),
        "truck": (84, 214, 199),
        "alert": (2, 10, 242),
        "other": (255, 255, 255),
    }

    def __init__(
        self,
        rois: List[List[Tuple[int, int]]],
        frames: List[Dict[str, Any]],
        width: int,
        height: int,
        cam_name: str,
    ) -> None:
        super().__init__(rois)

        # check if out dir exists else create
        if not os.path.exists("out"):
            os.mkdir("out")

        # initialize video
        self.video = cv2.VideoWriter(
            "out/output.mp4",
            cv2.VideoWriter_fourcc(*"mp4v"),
            5,
            (width, height),
        )

        self.frames = frames
        self.cam_name = cam_name

    def __add_polygon(self, img, polygon, obj_class) -> None:
        """This function is used to add polygon to the provided image (np.array)"""
        vertices = np.array(polygon.exterior.coords, np.int32)
        cv2.polylines(img, [vertices], True, self.__colors[obj_class], 2)

    def __add_dot(self, img, dot, obj_class) -> None:
        """This function is used to add dot to the provided image (np.array)"""
        vertices = np.array(dot.coords, np.int32)
        cv2.circle(
            img,
            vertices[0],
            radius=3,
            color=self.__colors[obj_class],
            thickness=-1,
        )

    def render(self):
        """Handles the main event loop and renders video"""

        for frame in self.frames:
            alerts: Set[int] = set()
            image = np.zeros((height, width, 3), dtype=np.uint8)

            # go through each detection in the frame
            # and add it to the frame image
            for detection in frame["detections"]:
                obj_class: str = detection.get("class")
                object = Polygon(
                    self.calculate_coords(
                        **detection["bbox"],
                        frame_width=frame["frame_width"],
                        frame_height=frame["frame_height"],
                    )
                )

                # event detection
                # self.execute returns the index of region of
                # interest where the rule states positive else returns -1
                alerts.add(
                    self.execute(
                        object=object,
                        obj_class=obj_class,
                        frame_num=frame["frame_num"],
                        timestamp=frame["timestamp"],
                        cam_name=self.cam_name,
                    )
                )

                # render objects
                self.__add_polygon(image, object, detection["class"])
                self.__add_dot(image, object.centroid, detection["class"])

            # draw region of interests to the image
            indicator: bool = False
            for index, roi in enumerate(self._rois):
                color: str = "other"
                if index in alerts:
                    indicator = True
                    color = "alert"

                self.__add_polygon(image, roi, color)

            # add border
            color: str = "alert" if indicator else "other"
            self.__add_polygon(
                image,
                Polygon([(0, 0), (width, 0), (width, height), (0, height)]),
                color,
            )

            # draw frame number
            cv2.putText(
                image,
                f"frame: {frame['frame_num']}",
                (50, 50),
                cv2.FONT_HERSHEY_SIMPLEX,
                1,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
            self.video.write(image)

            # reset event detection for next frame
            self.reset_objects()

        self.video.release()


if __name__ == "__main__":
    cam_name: str
    width: int = 1920
    height: int = 1080

    # parse frames
    frames: List[Dict[str, Any]] = list()
    with open("annotations.json", "r") as f:
        content = json.loads(f.read())
        cam_name = content["cam_name"]
        frames = content["frames"]

    root = Render(
        rois=[
            [(885, 85), (834, 246), (1228, 260), (1139, 77)],
            [(181, 288), (165, 522), (612, 510), (544, 246)],
        ],
        frames=frames,
        width=width,
        height=height,
        cam_name=cam_name,
    )

    root.render()
