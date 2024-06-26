import logging

from datetime import datetime
import numpy as np
import time
from os import listdir
from os.path import isfile, join
from threading import Thread
from typing import cast

import random
import cv2

from finch.brush import (
    BrushSet,
    preload_brush_textures_for_brush_set,
)
from finch.difference_image import DifferenceMethod
from finch.fitness import get_fitness
from finch.generate import get_initial_specimen, iterate_image, is_drawing_finished
from finch.image_gradient import ImageGradient
from finch.primitive_types import FitnessScore, Image
from finch.interface import get_window_size, render_thread
from finch.scale import scale_to_dimension
from finch.shared_state import State

logger = logging.getLogger(__name__)

MAXIMUM_TIME_PER_IMAGE_SECONDS = 10 * 60
MINIMUM_STEP_TIME_SECONDS = 0.0001
WAIT_BETWEEN_IMAGES_SECONDS = 1 * 60
DIFF_METHOD = DifferenceMethod.DELTAE
FULLSCREEN = True


def run_continuous_finch(image_folder: str, brush_sets: list[BrushSet]):
    n_iterations_with_same_score = 0
    last_update_time = datetime.now()

    shared_state = _initial_shared_state_object()

    thread = Thread(
        target=render_thread,
        name="rendering_thread",
        kwargs={
            "shared_state": shared_state,
            "fullscreen": FULLSCREEN,
        },
    )
    thread.start()
    time.sleep(0.1)

    while not shared_state.flag_stop:
        target_image, target_gradient, fitness = _initialize_for_next_image(image_folder, brush_sets, shared_state)

        generation_index = 0
        image_start_time = time.time()

        while (
            not shared_state.flag_stop
            and not shared_state.flag_next_image
            and (shared_state.lock_image or time.time() - image_start_time < MAXIMUM_TIME_PER_IMAGE_SECONDS)
        ):
            frame_start_time = time.time()
            generation_index += 1

            new_specimen, new_fitness, new_score = iterate_image(
                shared_state.specimen,
                fitness,
                target_image,
                target_gradient,
                store_brushes=False,
                diff_method=DIFF_METHOD,
            )

            # Only keep the new version if it is an improvement
            if new_score >= shared_state.score:
                n_iterations_with_same_score += 1
            else:
                n_iterations_with_same_score = 0
                fitness = new_fitness
                shared_state.score = new_score
                shared_state.specimen = new_specimen
                shared_state.image_available = True

            current_update_time = datetime.now()
            shared_state.update_time_microseconds = (current_update_time - last_update_time).microseconds
            last_update_time = current_update_time

            report_string = (
                f"gen_{generation_index:06d}__dt_{shared_state.update_time_microseconds}_us__score_{shared_state.score}"
            )

            logger.debug(report_string)

            if not shared_state.lock_image and is_drawing_finished(n_iterations_with_same_score, shared_state.score):
                break

            frame_time = time.time() - frame_start_time
            if frame_time < MINIMUM_STEP_TIME_SECONDS:
                time.sleep(MINIMUM_STEP_TIME_SECONDS - frame_time)

        _wait_for_next_image(shared_state)
        shared_state.flag_next_image = False

    thread.join()


def _initial_shared_state_object() -> State:
    """Create some initial shared state object - this will be overwritten before starting anyway"""
    empty_image: Image = np.zeros((0,0,1))
    return State(
        img_path="",
        brush=BrushSet.Canvas,
        target_image=empty_image,
        specimen=get_initial_specimen(empty_image, is_placeholder=True),
    )


def _initialize_for_next_image(
    image_folder, brush_sets, shared_state: State
) -> tuple[Image, ImageGradient, FitnessScore]:
    shared_state.img_path = _get_random_image_path(image_folder, shared_state.img_path)
    shared_state.brush = random.choice(brush_sets)
    preload_brush_textures_for_brush_set(brush_set=shared_state.brush)

    logger.info(f"Drawing image {shared_state.img_path}")
    target_image, target_gradient = _prep_image(shared_state.img_path)
    shared_state.target_image = target_image
    if shared_state.specimen.is_placeholder:
        shared_state.specimen = get_initial_specimen(target_image=target_image)
        shared_state.specimen.is_placeholder = False
    shared_state.score = 9999999

    return (
        target_image,
        target_gradient,
        get_fitness(specimen=shared_state.specimen, target_image=target_image, diff_method=DIFF_METHOD),
    )


def _get_random_image_path(image_folder: str, previous: str | None) -> str:
    img_paths = [join(image_folder, f) for f in listdir(image_folder) if isfile(join(image_folder, f))]
    img_path = previous
    while img_path == previous:
        img_path = random.choice(img_paths)
    return cast(str, img_path)


def _prep_image(img_path: str) -> tuple[Image, ImageGradient]:
    image = cv2.imread(img_path)
    dimension = get_window_size(use_full_monitor=FULLSCREEN)
    image = scale_to_dimension(image, dimension)
    image = cv2.blur(image, (5, 5))
    return image, ImageGradient(image=image)


def _wait_for_next_image(shared_state: State) -> None:
    """
    Semi-active wait loop to ensure that we can still interact (lock/unlock, switch to next image, quit the program)
    during the wait.
    """
    logger.info(f"Waiting for {WAIT_BETWEEN_IMAGES_SECONDS} before drawing next image")
    wait_start = time.time()
    while shared_state.lock_image or (
        time.time() - wait_start < WAIT_BETWEEN_IMAGES_SECONDS
        and not shared_state.flag_stop
        and not shared_state.flag_next_image
    ):
        time.sleep(1)
