"""
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2017  Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
"""

import abc
import logging  # TODO: logging is somewhat redundant here
import multiprocessing as mp
import os
from fractions import Fraction
from glob import glob

import av
from pyglui import ui

import background_helper as bh
import csv_utils
import player_methods as pm
from plugin import Analysis_Plugin_Base
from video_capture import File_Source, EndofVideoError

logger = logging.getLogger(__name__)


__version__ = 2


class Empty(object):
    pass


def export_processed_h264(
    world_timestamps,
    unprocessed_video_loc,
    target_video_loc,
    export_range,
    process_frame,
):
    yield "Converting video", .1
    capture = File_Source(Empty(), unprocessed_video_loc)
    if not capture.initialised:
        yield "Converting scene video failed", 0.
        return

    export_window = pm.exact_window(world_timestamps, export_range)
    (export_from_index, export_to_index) = pm.find_closest(
        capture.timestamps, export_window
    )
    print(export_range, export_from_index, export_to_index)

    update_rate = 10
    start_time = None
    time_base = Fraction(1, 65535)

    target_container = av.open(target_video_loc, "w")
    video_stream = target_container.add_stream("mpeg4", 1 / time_base)
    video_stream.bit_rate = 150e6
    video_stream.bit_rate_tolerance = video_stream.bit_rate / 20
    video_stream.thread_count = max(1, mp.cpu_count() - 1)
    video_stream.width, video_stream.height = capture.frame_size

    av_frame = av.VideoFrame(*capture.frame_size, "bgr24")
    av_frame.time_base = time_base

    capture.seek_to_frame(export_from_index)
    next_update_idx = export_from_index + update_rate
    while True:
        try:
            frame = capture.get_frame()
        except EndofVideoError:
            break

        if frame.index > export_to_index:
            break

        if start_time is None:
            start_time = frame.timestamp

        undistorted_img = process_frame(capture, frame)
        av_frame.planes[0].update(undistorted_img)
        av_frame.pts = int((frame.timestamp - start_time) / time_base)

        packet = video_stream.encode(av_frame)
        if packet:
            target_container.mux(packet)

        if capture.current_frame_idx >= next_update_idx:
            progress = (
                (capture.current_frame_idx - export_from_index)
                / (export_to_index - export_from_index)
            ) * .9 + .1
            yield "Converting video", progress * 100.
            next_update_idx += update_rate

    while True:  # flush encoder
        packet = video_stream.encode()
        if packet:
            target_container.mux(packet)
        else:
            break

    target_container.close()
    capture.cleanup()
    yield "Converting video completed", 1. * 100.


class VideoExporter(Analysis_Plugin_Base):
    """iMotions Gaze and Video Exporter

    All files exported by this plugin are saved to a subdirectory within
    the export directory called "iMotions". The gaze data will be written
    into a file called "gaze.tlv" and the undistored scene video will be
    saved in a file called "scene.mp4".

    The gaze.tlv file is a tab-separated CSV file with the following fields:
        GazeTimeStamp: Timestamp of the gaze point, unit: seconds
        MediaTimeStamp: Timestamp of the scene frame to which the gaze point
                        was correlated to, unit: seconds
        MediaFrameIndex: Index of the scene frame to which the gaze point was
                         correlated to
        Gaze3dX: X position of the 3d gaze point (the point the subject looks
                 at) in the scene camera coordinate system
        Gaze3dY: Y position of the 3d gaze point
        Gaze3dZ: Z position of the 3d gaze point
        Gaze2dX: undistorted gaze pixel postion, X coordinate, unit: pixels
        Gaze2dX: undistorted gaze pixel postion, Y coordinate, unit: pixels
        PupilDiaLeft: Left pupil diameter, 0.0 if not available, unit: millimeters
        PupilDiaRight: Right pupil diameter, 0.0 if not available, unit: millimeters
        Confidence: Value between 0 and 1 indicating the quality of the gaze
                    datum. It depends on the confidence of the pupil detection
                    and the confidence of the 3d model. Higher values are good.
    """

    __metaclass__ = abc.ABCMeta

    def __init__(self, g_pool):
        super().__init__(g_pool)
        self.export_tasks = []  # TODO: a task name is probably not required here
        self.status = "Not exporting"
        self.progress = 0.
        self.output = "Not set yet"

    def on_notify(self, notification):
        if notification["subject"] == "should_export":
            self.cancel()
            self.export_data(notification["range"], notification["export_dir"])

    @abc.abstractmethod
    def customize_menu(self):
        pass

    def init_ui(self):
        self.add_menu()
        self.customize_menu()
        self.menu.append(
            ui.Text_Input("status", self, label="Status", setter=lambda _: None)
        )
        self.menu.append(
            ui.Text_Input("output", self, label="Last export", setter=lambda _: None)
        )
        self.menu.append(ui.Slider("progress", self, label="Progress"))
        self.menu[-1].read_only = True
        self.menu[-1].display_format = "%.0f%%"
        self.menu.append(ui.Button("Cancel export", self.cancel))

    def cancel(self):
        for task_name, task in self.export_tasks:
            task.cancel()
        self.export_tasks = []

    def deinit_ui(self):
        self.remove_menu()

    def cleanup(self):
        self.cancel()

    def add_export_job(
        self,
        export_range,
        export_dir,
        plugin_name,
        input_name,
        output_name,
        process_frame,
    ):
        rec_start = self.get_recording_start_date()
        im_dir = os.path.join(export_dir, plugin_name + "_{}".format(rec_start))
        os.makedirs(im_dir, exist_ok=True)
        self.output = im_dir
        logger.info("Exporting to {}".format(im_dir))

        distorted_video_loc = [
            f
            for f in glob(os.path.join(self.g_pool.rec_dir, input_name + ".*"))
            if os.path.splitext(f)[-1] in (".mp4", ".mkv", ".avi", ".mjpeg")
        ][0]
        target_video_loc = os.path.join(im_dir, output_name + ".mp4")
        generator_args = (
            self.g_pool.timestamps,
            distorted_video_loc,
            target_video_loc,
            export_range,
            process_frame,
        )
        task = bh.Task_Proxy(
            plugin_name + " Video Export", export_processed_h264, args=generator_args
        )
        self.export_tasks.append(("taskname", task))
        return {"export_folder": im_dir}

    @abc.abstractmethod
    def export_data(self, export_range, export_dir):
        pass

    def recent_events(self, events):
        for task_name, task in self.export_tasks:
            recent = [d for d in task.fetch()]
            if recent:
                self.status, self.progress = recent[-1]
            if task.canceled:
                # TODO: in case of multiple tasks make sure that all are canceled?!
                self.status = "Export has been canceled"
                self.progress = 0.

    def gl_display(self):
        self.menu_icon.indicator_stop = self.progress / 100.

    def get_recording_start_date(self):
        csv_loc = os.path.join(self.g_pool.rec_dir, "info.csv")
        with open(csv_loc, "r", encoding="utf-8") as csvfile:
            rec_info = csv_utils.read_key_value_file(csvfile)
            date = rec_info["Start Date"].replace(".", "_").replace(":", "_")
            time = rec_info["Start Time"].replace(":", "_")
        return "{}_{}".format(date, time)
