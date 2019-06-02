#!/usr/bin/env python3

import cv2
import numpy as np
from PIL import ImageFont, ImageDraw, Image
import paho.mqtt.client as mqtt
from util import log, error, warning, info, success as u
from functools import partial, wraps
from Layer import Layer, RelativeLayer, AbsoluteLayer, Layer2
from Detector import Detector

def draw_polylines(frame, dst_arr, color=(0, 0, 255), thickness=3):
    return cv2.polylines(frame, dst_arr, True, color, thickness)

class ImageProcessor(object):
    def __init__(self, cap, machine, opts=dict()):                
        if not isinstance(opts, dict):
            error("Options must be an dictionary")
            exit()

        reference_image = machine.get_ref()
        reference_image = cv2.imread(reference_image)
        gray_reference_image = cv2.cvtColor(reference_image, cv2.COLOR_BGR2GRAY)

        global LED_STATE
        self.cap = cap
        self.max_width = 1280
        self.max_height = 1280
        self.client = None
        self.reference_image = reference_image
        self.gray_reference_image = gray_reference_image
        self.fps = int(1000/1000)
        self.descritor_algorithm = "ORB"
        self.matcher_algorithm = "FLANN"
        self.homography = None
        self.follow_object = lambda frame, *a: frame
        self.window_name = "Homography"
        self.layers = []
        self.detector = Detector(machine)
        self.run = False
        self.ref_height, self.ref_width = self.reference_image.shape[:2]
        self.handlers = []
        self.cam_matrix = np.loadtxt("mtx.txt")

        self.frame_width = int(self.cap.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.frame_height = int(self.cap.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        self.scene_height, self.scene_width = \
            ImageProcessor.calc_scene_size((self.frame_height, self.frame_width),
                                           (self.max_height, self.max_width))

        self.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))

        self.kernel = np.array([[-1,-1,-1], 
                                [-1, 9,-1],
                                [-1,-1,-1]])

        ## Create descriptor
        self.create_descriptor(self.descritor_algorithm)
        ## Generate keypoints and descriptors for reference image
        self.ref_kp, self.ref_desc = self.prepare_reference()
        ## Create keypoints matcher
        self.create_matcher(self.matcher_algorithm)


    def start(self):
        h = self.ref_height
        w = self.ref_width
        this = self
        self.run = True
        homography = None
        good_points = []

        while self.run:
            ret, frame = self.cap.read()
            frame = cv2.resize(frame, (self.scene_width, self.scene_height))
            kp, desc = self.detect_frame(frame)

            if desc is None:
                self.show_img(frame)
                key = cv2.waitKey(self.fps)
                if key==27:
                    self.stop()
                continue

            good_points = self.calc_good_points(desc)

            if len(good_points) > 35:
                matrix, mask = self.find_homography(kp, good_points)
                matches_mask = mask.ravel().tolist()

                # Perspective transform
                pts = np.float32([[[0, 0]], [[0, h]], [[w, h]], [[w, 0]]])
                dst = cv2.perspectiveTransform(pts, matrix)
                dst_arr = [np.int32(dst)]

                homography = self.follow_object(frame, dst_arr)
                homography = self.draw_layers(homography, matrix, mask,
                                              (self.scene_height, self.scene_width))
                # homography = cv2.resize(homography, (1280, 960))
                self.show_img(homography)
            else:
                self.show_img(frame)

            key = cv2.waitKey(self.fps)
            if key==27:
                self.stop()

        self.cap.release()

    def stop(self):
        self.run = False

    def create_descriptor(self, algorithm):
        if algorithm == "ORB":
            fn = cv2.ORB_create
        else:
            fn = cv2.ORB_create
        self.descriptor = fn(1000)

    def prepare_reference(self):
        return self.descriptor.detectAndCompute(self.gray_reference_image, None)

    def create_matcher(self, matcher_algorithm, index_params=None, search_params=None):
        index_params = (index_params or
                        dict(algorithm =6,           # FLANN_INDEX_LSH
                             table_number = 12,       # 12
                             key_size = 20,          # 20
                             multi_probe_level = 1)) # 2
        search_params = search_params or dict(checks=300)
        if matcher_algorithm == "FLANN":
            matcher = cv2.FlannBasedMatcher
        else:
            matcher = cv2.FlannBasedMatcher
        
        self.matcher = matcher(index_params, search_params)

    def follow_object_fn(self, frame, dst_arr):
        return draw_polylines(frame, dst_arr)

    def enable_object_follow(self):
        self.follow_object = self.follow_object_fn

    def show_img(self, frame, name=None):
        cv2.imshow(name or self.window_name, frame)

    def detect_frame(self, frame):
        gframe = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # gframe = cv2.equalizeHist(gframe)
        # gframe = self.clahe.apply(gframe)
        # gframe = cv2.filter2D(gframe, -1, self.kernel)
        return self.descriptor.detectAndCompute(gframe, None)

    def calc_good_points(self, desc):
        matches = self.matcher.knnMatch(self.ref_desc, desc, k=2)
        good_points = []
        for mn in matches:
                if len(mn) != 2: continue

                m, n = mn
                if m.distance < 0.85 * n.distance:
                    good_points.append(m)
        return good_points

    def find_homography(self, kp, good_points):
        qpts = np.float32([self.ref_kp[m.queryIdx].pt for m in good_points]).reshape(-1, 1, 2)
        tpts = np.float32([kp[m.trainIdx].pt for m in good_points]).reshape(-1, 1, 2)
        return cv2.findHomography(qpts, tpts, cv2.RANSAC, 5.0)

    def create_layer(self, name=None, draw=None, transform=None, follow=None):
        name = name if name else "Layer " + str(len(Layer.all_layers))
        transform = transform or (transform is None and type == "relative")

        if transform:
            image_size = (self.ref_height, self.ref_width)
        else:
            image_size = (self.scene_height, self.scene_width)

        layer = Layer2(name,
                      draw=partial(ImageProcessor.draw, draw, self),
                      size=image_size,
                      transform=transform,
                      follow=follow,
                      ref_size = (self.ref_height, self.ref_width))

        self.layers.append(layer)
        return layer

    def draw_layers(self, homography, matrix, mask, size):
        height, width = size
        for layer in self.layers:
            should_transform = layer.transform
   
            if not should_transform and layer.follow:
                result = layer.draw(matrix)
            else:
                result = layer.draw()
            if should_transform:
                result = cv2.warpPerspective(result, matrix, (width, height))

            r, c, ch = result.shape
            roi = homography[0:r, 0:c]

            gray_result = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
            ret, mask = cv2.threshold(gray_result, 10, 255, cv2.THRESH_BINARY)
            mask_inv = cv2.bitwise_not(mask)

            homography_bg = cv2.bitwise_and(roi, roi, mask=mask_inv)
            img_fg = cv2.bitwise_and(result, result, mask=mask)

            homography = cv2.addWeighted(homography_bg, 1,  img_fg, 1, gamma=0)
        
        return homography

    def set_client(self, client):
        self.client = client

    def set_handler(self, event, handler):
        self.handlers.append(event, partial(handler, self.parameters))

    def delete_parameter(self, parameter):
        del self.parameters[parameter]

    def get_parameters(self):
        return self.client.get_parameters()

    @staticmethod
    def calc_scene_size(frame_size, max_size):
        height, width = frame_size
        max_height, max_width = max_size

        if max_width and width > max_width:
            scale = max_width/width
            width = int(width*scale)
            height = int(height*scale)
        if max_height and height > max_height:
            scale = max_height/height
            width = int(width*scale)
            height = int(height*scale)
        return height, width

    @staticmethod
    def draw(draw_fn, self, x, y=None):
        if y is not None:
            return draw_fn(self.get_parameters(), x, matrix=y)
        else:
            return draw_fn(self.get_parameters(), x)


################
## Brute force detection
################

# ## ORB DETECTOR
# orb = cv2.ORB_create()
# kp1, desc1 = orb.detectAndCompute(org, None)
# kp2, desc2 = orb.detectAndCompute(i1, None)

# ## Brute Force matching
# bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
# matches = bf.match(desc1, desc2)
# matches = sorted(matches, key=lambda x:x.distance)

# res = cv2.drawMatches(org, kp1, i1, kp2, matches[:50], None, flags=2)

# cv2.imshow("Match", res)

################


################
## DETECTIONS ONLY
################

# sift = cv2.xfeatures2d.SURF_create()
# orb = cv2.ORB_create()

# kp, _ = orb.detectAndCompute(org, None)

# org = cv2.drawKeypoints(org, kp, None)

# cv2.imshow("Original", org)

################

if __name__ == "__main__":

    # cap = cv2.VideoCapture(0)

    # processStream(cap)



    base = cv2.imread("/home/rafcio/Mgr/img/014.png")
    # base = cv2.resize(base, (240, 320))

    ## Features
    sift = cv2.xfeatures2d.SIFT_create()
    kp1, desc1 = sift.detectAndCompute(cv2.cvtColor(base, cv2.COLOR_BGR2GRAY), None)
    base = cv2.drawKeypoints(base, kp1, base)

    cv2.imshow("IMG", base)

    cv2.waitKey(0)
    cv2.destroyAllWindows()