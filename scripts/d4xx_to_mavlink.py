#!/usr/bin/env python3

######################################################
##  librealsense D4xx to MAVLink                    ##
######################################################
# Requirements: 
#   x86 based Companion Computer (for compatibility with Intel),
#   Ubuntu 18.04 (otherwise, the following installation instruction might not work),
#   Python3 (default with Ubuntu 18.04)
# Install required packages: 
#   pip3 install pyrealsense2
#   pip3 install transformations
#   pip3 install dronekit
#   pip3 install apscheduler
#   pip3 install pyserial
#   pip3 install numba
#   pip3 install opencv-python
# Only necessary if you installed the minimal version of Ubuntu:
#   sudo apt install python3-opencv

# Set the path for pyrealsense2.[].so
# Otherwise, place the pyrealsense2.[].so file under the same directory as this script or modify PYTHONPATH
import sys
sys.path.append("/usr/local/lib/")

# Set MAVLink protocol to 2.
import os
os.environ["MAVLINK20"] = "1"

# Import the libraries
import pyrealsense2 as rs
import numpy as np
import transformations as tf
import math as m
import time
import argparse
import threading
import json
from time import sleep
from apscheduler.schedulers.background import BackgroundScheduler
from dronekit import connect, VehicleMode
from pymavlink import mavutil
from numba import njit

# In order to import cv2 under python3 when you also have ROS Kinetic installed
if os.path.exists("/opt/ros/kinetic/lib/python2.7/dist-packages"):
    sys.path.remove('/opt/ros/kinetic/lib/python2.7/dist-packages') 
if os.path.exists("~/anaconda3/lib/python3.7/site-packages"):
    sys.path.append('~/anaconda3/lib/python3.7/site-packages')
import cv2

######################################################
##  Depth parameters - reconfigurable               ##
######################################################

# Sensor-specific parameter, for D435: https://www.intelrealsense.com/depth-camera-d435/
STREAM_TYPE = [rs.stream.depth, rs.stream.color]  # rs2_stream is a types of data provided by RealSense device
FORMAT      = [rs.format.z16, rs.format.bgr8]     # rs2_format is identifies how binary data is encoded within a frame
DEPTH_WIDTH = 640               # Defines the number of columns for each frame or zero for auto resolve
DEPTH_HEIGHT= 480               # Defines the number of lines for each frame or zero for auto resolve
FPS         = 30                # Defines the rate of frames per second
DEPTH_RANGE_M = [0.1, 8.0]        # Replace with your sensor's specifics, in meter

obstacle_line_height_ratio = 0.25  # [0-1]: 0-Top, 1-Bottom. The height of the horizontal line to find distance to obstacle.
obstacle_line_thickness_pixel = 10 # [1-DEPTH_HEIGHT]: Number of pixel rows to use to generate the obstacle distance message. For each column, the scan will return the minimum value for those pixels centered vertically in the image.

USE_PRESET_FILE = True
PRESET_FILE  = "../cfg/d4xx-default.json"

# List of filters to be applied, in this order.
# https://github.com/IntelRealSense/librealsense/blob/master/doc/post-processing-filters.md

filters = [
    [True, "Decimation Filter",     rs.decimation_filter()],
    [True, "Threshold Filter",      rs.threshold_filter()],
    [True, "Depth to Disparity",    rs.disparity_transform(True)],
    [True, "Spatial Filter",        rs.spatial_filter()],
    [True, "Temporal Filter",       rs.temporal_filter()],
    [False, "Hole Filling Filter",   rs.hole_filling_filter()],
    [True, "Disparity to Depth",    rs.disparity_transform(False)]
]

#
# The filters can be tuned with opencv_depth_filtering.py script, and save the default values to here
# Individual filters have different options so one have to apply the values accordingly
#

# decimation_magnitude = 8
# filters[0][2].set_option(rs.option.filter_magnitude, decimation_magnitude)

threshold_min_m = DEPTH_RANGE_M[0]
threshold_max_m = DEPTH_RANGE_M[1]
if filters[1][0] is True:
    filters[1][2].set_option(rs.option.min_distance, threshold_min_m)
    filters[1][2].set_option(rs.option.max_distance, threshold_max_m)

######################################################
##  ArduPilot-related parameters - reconfigurable   ##
######################################################

# Default configurations for connection to the FCU
connection_string_default = '/dev/ttyUSB0'
connection_baudrate_default = 921600

# Use this to rotate all processed data
camera_facing_angle_degree = 0

# Enable/disable each message/function individually
enable_msg_obstacle_distance = True
enable_msg_distance_sensor = False
obstacle_distance_msg_hz_default = 15.0

# lock for thread synchronization
lock = threading.Lock()

debug_enable_default = 0

######################################################
##  Global variables                                ##
######################################################

# FCU connection variables
vehicle = None
is_vehicle_connected = False
vehicle_pitch_rad = None

# Camera-related variables
pipe = None
depth_scale = 0
colorizer = rs.colorizer()
depth_hfov_deg = None
depth_vfov_deg = None

# The name of the display window
display_name  = 'Input/output depth'

# Data variables
data = None
current_time_us = 0

# Obstacle distances in front of the sensor, starting from the left in increment degrees to the right
# See here: https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
min_depth_cm = int(DEPTH_RANGE_M[0] * 100)  # In cm
max_depth_cm = int(DEPTH_RANGE_M[1] * 100)  # In cm, should be a little conservative
distances_array_length = 72
angle_offset = None
increment_f  = None
distances = np.ones((distances_array_length,), dtype=np.uint16) * (max_depth_cm + 1)

######################################################
##  Parsing user' inputs                            ##
######################################################

parser = argparse.ArgumentParser(description='Reboots vehicle')
parser.add_argument('--connect',
                    help="Vehicle connection target string. If not specified, a default string will be used.")
parser.add_argument('--baudrate', type=float,
                    help="Vehicle connection baudrate. If not specified, a default value will be used.")
parser.add_argument('--obstacle_distance_msg_hz', type=float,
                    help="Update frequency for OBSTACLE_DISTANCE message. If not specified, a default value will be used.")
parser.add_argument('--debug_enable',type=float,
                    help="Enable debugging information")

args = parser.parse_args()

connection_string = args.connect
connection_baudrate = args.baudrate
obstacle_distance_msg_hz = args.obstacle_distance_msg_hz
debug_enable = args.debug_enable

# Using default values if no specified inputs
if not connection_string:
    connection_string = connection_string_default
    print("INFO: Using default connection_string", connection_string)
else:
    print("INFO: Using connection_string", connection_string)

if not connection_baudrate:
    connection_baudrate = connection_baudrate_default
    print("INFO: Using default connection_baudrate", connection_baudrate)
else:
    print("INFO: Using connection_baudrate", connection_baudrate)
    
if not obstacle_distance_msg_hz:
    obstacle_distance_msg_hz = obstacle_distance_msg_hz_default
    print("INFO: Using default obstacle_distance_msg_hz", obstacle_distance_msg_hz)
else:
    print("INFO: Using obstacle_distance_msg_hz", obstacle_distance_msg_hz)

# The list of filters to be applied on the depth image
for i in range(len(filters)):
    if filters[i][0] is True:
        print("INFO: Applying: ", filters[i][1])
    else:
        print("INFO: NOT applying: ", filters[i][1])

if not debug_enable:
    debug_enable = debug_enable_default

if debug_enable == 1:
    print("INFO: Debugging option enabled")
else:
    print("INFO: Debugging option DISABLED")

######################################################
##  Functions - MAVLink                             ##
######################################################

# https://mavlink.io/en/messages/common.html#OBSTACLE_DISTANCE
def send_obstacle_distance_message():
    global current_time_us, distances, camera_facing_angle_degree
    if angle_offset is None or increment_f is None:
        print("Please call set_obstacle_distance_params before continue")
    else:
        msg = vehicle.message_factory.obstacle_distance_encode(
            current_time_us,    # us Timestamp (UNIX time or time since system boot)
            0,                  # sensor_type, defined here: https://mavlink.io/en/messages/common.html#MAV_DISTANCE_SENSOR
            distances,          # distances,    uint16_t[72],   cm
            0,                  # increment,    uint8_t,        deg
            min_depth_cm,	    # min_distance, uint16_t,       cm
            max_depth_cm,       # max_distance, uint16_t,       cm
            increment_f,	    # increment_f,  float,          deg
            angle_offset,       # angle_offset, float,          deg
            12                  # MAV_FRAME, vehicle-front aligned: https://mavlink.io/en/messages/common.html#MAV_FRAME_BODY_FRD    
        )

        vehicle.send_mavlink(msg)
        vehicle.flush()

        #
        # Temporary fix to make the right-most range appear on MP's Proximity GUI
        #
        # Send the minimum distance of the 1/4 right-most part of the horizontal FOV to the orientation right next to the center
        end_point = distances_array_length - 1
        start_point = int(0.75 * end_point)
        min_rightmost_distance = min(distances[start_point:end_point])
        orientation = int(camera_facing_angle_degree / 45) + 1
        send_single_distance_sensor_msg(min_rightmost_distance, orientation)

# https://mavlink.io/en/messages/common.html#DISTANCE_SENSOR
def send_single_distance_sensor_msg(distance, orientation):
    # Average out a portion of the centermost part
    msg = vehicle.message_factory.distance_sensor_encode(
        0,                  # ms Timestamp (UNIX time or time since system boot) (ignored)
        min_depth_cm,       # min_distance, uint16_t, cm
        max_depth_cm,       # min_distance, uint16_t, cm
        distance,           # current_distance,	uint16_t, cm	
        0,	                # type : 0 (ignored)
        0,                  # id : 0 (ignored)
        orientation,        # orientation
        0                   # covariance : 0 (ignored)
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

# https://mavlink.io/en/messages/common.html#DISTANCE_SENSOR
def send_distance_sensor_message():
    global distances
    # Average out a portion of the centermost part
    curr_dist = int(np.mean(distances[33:38]))
    msg = vehicle.message_factory.distance_sensor_encode(
        0,# ms Timestamp (UNIX time or time since system boot) (ignored)
        min_depth_cm,   # min_distance, uint16_t, cm
        max_depth_cm,   # min_distance, uint16_t, cm
        curr_dist,      # current_distance,	uint16_t, cm	
        0,	            # type : 0 (ignored)
        0,              # id : 0 (ignored)
        int(camera_facing_angle_degree / 45),              # orientation
        0               # covariance : 0 (ignored)
    )

    vehicle.send_mavlink(msg)
    vehicle.flush()

def send_msg_to_gcs(text_to_be_sent):
    # MAV_SEVERITY: 0=EMERGENCY 1=ALERT 2=CRITICAL 3=ERROR, 4=WARNING, 5=NOTICE, 6=INFO, 7=DEBUG, 8=ENUM_END
    # Defined here: https://mavlink.io/en/messages/common.html#MAV_SEVERITY
    # MAV_SEVERITY = 3 will let the message be displayed on Mission Planner HUD, but 6 is ok for QGroundControl
    if is_vehicle_connected == True:
        text_msg = 'D4xx: ' + text_to_be_sent
        status_msg = vehicle.message_factory.statustext_encode(
            6,                      # MAV_SEVERITY
            text_msg.encode()	    # max size is char[50]       
        )
        vehicle.send_mavlink(status_msg)
        vehicle.flush()
        print("INFO: " + text_to_be_sent)
    else:
        print("INFO: Vehicle not connected. Cannot send text message to Ground Control Station (GCS)")

# Request a timesync update from the flight controller, for future work.
# TODO: Inspect the usage of timesync_update 
def update_timesync(ts=0, tc=0):
    if ts == 0:
        ts = int(round(time.time() * 1000))
    msg = vehicle.message_factory.timesync_encode(
        tc,     # tc1
        ts      # ts1
    )
    vehicle.send_mavlink(msg)
    vehicle.flush()

# Listen to ATTITUDE data: https://mavlink.io/en/messages/common.html#ATTITUDE
def att_msg_callback(self, attr_name, value):
    global vehicle_pitch_rad
    vehicle_pitch_rad = value.pitch
    if debug_enable == 1:
        print("INFO: Received ATTITUDE msg, current pitch is %.2f" % m.degrees(vehicle_pitch_rad), "degrees")

# Listen to AHRS2 data: https://mavlink.io/en/messages/ardupilotmega.html#AHRS2
def ahrs2_msg_callback(self, attr_name, value):
    global vehicle_pitch_rad
    vehicle_pitch_rad = value.pitch
    if debug_enable == 1:
        print("INFO: Received AHRS2 msg, current pitch is %.2f" % m.degrees(vehicle_pitch_rad), "degrees")

# Establish connection to the FCU
def vehicle_connect():
    global vehicle, is_vehicle_connected

    if vehicle == None:
        try:
            vehicle = connect(connection_string, wait_ready = True, baud = connection_baudrate, source_system = 1)
        except:
            print('Connection error! Retrying...')
            sleep(1)

    if vehicle == None:
        is_vehicle_connected = False
        return False
    else:
        is_vehicle_connected = True
        return True

######################################################
##  Functions - D4xx cameras                        ##
######################################################

DS5_product_ids = ["0AD1", "0AD2", "0AD3", "0AD4", "0AD5", "0AF6", "0AFE", "0AFF", "0B00", "0B01", "0B03", "0B07","0B3A"]

def find_device_that_supports_advanced_mode() :
    ctx = rs.context()
    ds5_dev = rs.device()
    devices = ctx.query_devices();
    for dev in devices:
        if dev.supports(rs.camera_info.product_id) and str(dev.get_info(rs.camera_info.product_id)) in DS5_product_ids:
            if dev.supports(rs.camera_info.name):
                print("INFO: Found device that supports advanced mode:", dev.get_info(rs.camera_info.name))
            return dev
    raise Exception("No device that supports advanced mode was found")

# Loop until we successfully enable advanced mode
def realsense_enable_advanced_mode(advnc_mode):
    while not advnc_mode.is_enabled():
        print("INFO: Trying to enable advanced mode...")
        advnc_mode.toggle_advanced_mode(True)
        # At this point the device will disconnect and re-connect.
        print("INFO: Sleeping for 5 seconds...")
        time.sleep(5)
        # The 'dev' object will become invalid and we need to initialize it again
        dev = find_device_that_supports_advanced_mode()
        advnc_mode = rs.rs400_advanced_mode(dev)
        print("INFO: Advanced mode is", "enabled" if advnc_mode.is_enabled() else "disabled")

# Load the settings stored in the JSON file
def realsense_load_settings_file(advnc_mode, setting_file):
    # Sanity checks
    if os.path.isfile(setting_file):
        print("INFO: Setting file found", setting_file)
    else:
        print("INFO: Cannot find setting file ", setting_file)
        exit()

    if advnc_mode.is_enabled():
        print("INFO: Advanced mode is enabled")
    else:
        print("INFO: Device does not support advanced mode")
        exit()
    
    # Input for load_json() is the content of the json file, not the file path
    with open(setting_file, 'r') as file:
        json_text = file.read().strip()

    advnc_mode.load_json(json_text)

# Establish connection to the Realsense camera
def realsense_connect():
    global pipe, depth_scale
    # Declare RealSense pipe, encapsulating the actual device and sensors
    pipe = rs.pipeline()

    # Configure depth and color streams
    config = rs.config()
    config.enable_stream(STREAM_TYPE[0], DEPTH_WIDTH, DEPTH_HEIGHT, FORMAT[0], FPS)
    if debug_enable == 1:
        config.enable_stream(STREAM_TYPE[1], DEPTH_WIDTH, DEPTH_HEIGHT, FORMAT[1], FPS)

    # Start streaming with requested config
    profile = pipe.start(config)

    # Getting the depth sensor's depth scale (see rs-align example for explanation)
    depth_sensor = profile.get_device().first_depth_sensor()
    depth_scale = depth_sensor.get_depth_scale()
    print("INFO: Depth scale is: ", depth_scale)

def realsense_configure_setting(setting_file):
    device = find_device_that_supports_advanced_mode()
    advnc_mode = rs.rs400_advanced_mode(device)
    realsense_enable_advanced_mode(advnc_mode)
    realsense_load_settings_file(advnc_mode, setting_file)

# Setting parameters for the OBSTACLE_DISTANCE message based on actual camera's intrinsics and user-defined params
def set_obstacle_distance_params():
    global angle_offset, camera_facing_angle_degree, increment_f, depth_scale, depth_hfov_deg, depth_vfov_deg, obstacle_line_height_ratio, obstacle_line_thickness_pixel
    
    # Obtain the intrinsics from the camera itself
    profiles = pipe.get_active_profile()
    depth_intrinsics = profiles.get_stream(STREAM_TYPE[0]).as_video_stream_profile().intrinsics
    print("INFO: Depth camera intrinsics: ", depth_intrinsics)
    
    # For forward facing camera with a horizontal wide view:
    #   HFOV=2*atan[w/(2.fx)],
    #   VFOV=2*atan[h/(2.fy)],
    #   DFOV=2*atan(Diag/2*f),
    #   Diag=sqrt(w^2 + h^2)
    depth_hfov_deg = m.degrees(2 * m.atan(DEPTH_WIDTH / (2 * depth_intrinsics.fx)))
    depth_vfov_deg = m.degrees(2 * m.atan(DEPTH_HEIGHT / (2 * depth_intrinsics.fy)))
    print("INFO: Depth camera HFOV: %0.2f degrees" % depth_hfov_deg)
    print("INFO: Depth camera VFOV: %0.2f degrees" % depth_vfov_deg)

    angle_offset = camera_facing_angle_degree - (depth_hfov_deg / 2)
    increment_f = depth_hfov_deg / distances_array_length
    print("INFO: OBSTACLE_DISTANCE angle_offset: %0.3f" % angle_offset)
    print("INFO: OBSTACLE_DISTANCE increment_f: %0.3f" % increment_f)
    print("INFO: OBSTACLE_DISTANCE coverage: from %0.3f" % (angle_offset), "to %0.3f degrees" % (angle_offset + increment_f * distances_array_length))

    # Sanity check for depth configuration
    if obstacle_line_height_ratio < 0 or obstacle_line_height_ratio > 1:
        print("Please make sure the horizontal position is within [0-1]: ", obstacle_line_height_ratio)
        sys.exit()

    if obstacle_line_thickness_pixel < 1 or obstacle_line_thickness_pixel > DEPTH_HEIGHT:
        print("Please make sure the thickness is within [0-DEPTH_HEIGHT]: ", obstacle_line_thickness_pixel)
        sys.exit()

# Find the height of the horizontal line to calculate the obstacle distances
#   - Basis: depth camera's vertical FOV, user's input
#   - Compensation: vehicle's current pitch angle
def find_obstacle_line_height():
    global vehicle_pitch_rad, depth_vfov_deg, DEPTH_HEIGHT

    # Basic position
    obstacle_line_height = DEPTH_HEIGHT * obstacle_line_height_ratio

    # Compensate for the vehicle's pitch angle if data is available
    if vehicle_pitch_rad is not None and depth_vfov_deg is not None:
        delta_height = m.sin(vehicle_pitch_rad / 2) / m.sin(m.radians(depth_vfov_deg) / 2) * DEPTH_HEIGHT
        obstacle_line_height += delta_height

    # Sanity check
    if obstacle_line_height < 0:
        obstacle_line_height = 0
    elif obstacle_line_height > DEPTH_HEIGHT:
        obstacle_line_height = DEPTH_HEIGHT
    
    return obstacle_line_height

# Calculate the distances array by dividing the FOV (horizontal) into $distances_array_length rays,
# then pick out the depth value at the pixel corresponding to each ray. Based on the definition of
# the MAVLink messages, the invalid distance value (below MIN/above MAX) will be replaced with MAX+1.
#    
# [0]    [35]   [71]    <- Output: distances[72]
#  |      |      |      <- step = width / 72
#  ---------------      <- horizontal line, or height/2
#  \      |      /
#   \     |     /
#    \    |    /
#     \   |   /
#      \  |  /
#       \ | /           
#       Camera          <- Input: depth_mat, obtained from depth image
#
# Note that we assume the input depth_mat is already processed by at least hole-filling filter.
# Otherwise, the output array might not be stable from frame to frame.
@njit
def distances_from_depth_image(obstacle_line_height, depth_mat, distances, min_depth_m, max_depth_m, obstacle_line_thickness_pixel):
    # Parameters for depth image
    depth_img_width  = depth_mat.shape[1]
    depth_img_height = depth_mat.shape[0]

    # Parameters for obstacle distance message
    step = depth_img_width / distances_array_length

    for i in range(distances_array_length):
        # Each range (left to right) is found from a set of rows within a column
        #  [ ] -> ignored
        #  [x] -> center + obstacle_line_thickness_pixel / 2
        #  [x] -> center = obstacle_line_height (moving up and down according to the vehicle's pitch angle)
        #  [x] -> center - obstacle_line_thickness_pixel / 2
        #  [ ] -> ignored
        #   ^ One of [distances_array_length] number of columns, from left to right in the image
        center_pixel = obstacle_line_height
        upper_pixel = center_pixel + obstacle_line_thickness_pixel / 2
        lower_pixel = center_pixel - obstacle_line_thickness_pixel / 2

        # Sanity checks
        if upper_pixel > depth_img_height:
            upper_pixel = depth_img_height
        elif upper_pixel < 1:
            upper_pixel = 1
        if lower_pixel > depth_img_height:
            lower_pixel = depth_img_height - 1
        elif lower_pixel < 0:
            lower_pixel = 0

        # Converting depth from uint16_t unit to metric unit. depth_scale is usually 1mm following ROS convention.
        # dist_m = depth_mat[int(obstacle_line_height), int(i * step)] * depth_scale
        min_point_in_scan = min(depth_mat[int(lower_pixel):int(upper_pixel), int(i * step)])
        dist_m = min_point_in_scan * depth_scale

        # Default value, unless overwritten: 
        #   A value of max_distance + 1 (cm) means no obstacle is present. 
        #   A value of UINT16_MAX (65535) for unknown/not used.
        distances[i] = 65535

        # Note that dist_m is in meter, while distances[] is in cm.
        if dist_m > min_depth_m and dist_m < max_depth_m:
            distances[i] = dist_m * 100

######################################################
##  Main code starts here                           ##
######################################################

print("INFO: Connecting to vehicle.")
while (not vehicle_connect()):
    pass
print("INFO: Vehicle connected.")

send_msg_to_gcs('Connecting to camera...')
if USE_PRESET_FILE:
    realsense_configure_setting(PRESET_FILE)
realsense_connect()
send_msg_to_gcs('Camera connected.')

set_obstacle_distance_params()

# Send MAVlink messages in the background at pre-determined frequencies
sched = BackgroundScheduler()

if enable_msg_obstacle_distance:
    sched.add_job(send_obstacle_distance_message, 'interval', seconds = 1/obstacle_distance_msg_hz)
    send_msg_to_gcs('Sending obstacle distance messages to FCU')
elif enable_msg_distance_sensor:
    sched.add_job(send_distance_sensor_message, 'interval', seconds = 1/obstacle_distance_msg_hz)
    send_msg_to_gcs('Sending distance sensor messages to FCU')
else:
    send_msg_to_gcs('Nothing to do. Check params to enable something')
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()

# Listen and extract necessary attitude's data
vehicle.add_message_listener('ATTITUDE', att_msg_callback)
# vehicle.add_message_listener('AHRS2', ahrs2_msg_callback)

sched.start()

# Begin of the main loop
last_time = time.time()
try:
    while True:
        # This call waits until a new coherent set of frames is available on a device
        # Calls to get_frame_data(...) and get_frame_timestamp(...) on a device will return stable values until wait_for_frames(...) is called
        frames = pipe.wait_for_frames()
        depth_frame = frames.get_depth_frame()

        if not depth_frame:
            continue

        # Store the timestamp for MAVLink messages
        current_time_us = int(round(time.time() * 1000000))

        # Apply the filters
        filtered_frame = depth_frame
        for i in range(len(filters)):
            if filters[i][0] is True:
                filtered_frame = filters[i][2].process(filtered_frame)

        # Extract depth in matrix form
        depth_data = filtered_frame.as_frame().get_data()
        depth_mat = np.asanyarray(depth_data)

        # Create obstacle distance data from depth image
        obstacle_line_height = find_obstacle_line_height()
        distances_from_depth_image(obstacle_line_height, depth_mat, distances, DEPTH_RANGE_M[0], DEPTH_RANGE_M[1], obstacle_line_thickness_pixel)

        if debug_enable == 1:
            # Prepare the data
            input_image = np.asanyarray(colorizer.colorize(depth_frame).get_data())
            output_image = np.asanyarray(colorizer.colorize(filtered_frame).get_data())
            display_image = np.hstack((input_image, cv2.resize(output_image, (DEPTH_WIDTH, DEPTH_HEIGHT))))

            # Draw a horizontal line to visualize the obstacles' line
            x1, y1 = int(DEPTH_WIDTH + obstacle_line_thickness_pixel / 2), int(obstacle_line_height)
            x2, y2 = int(DEPTH_WIDTH * 2), int(obstacle_line_height)
            line_thickness = obstacle_line_thickness_pixel
            cv2.line(display_image, (x1, y1), (x2, y2), (0, 255, 0), thickness=line_thickness)

            # Put the fps in the corner of the image
            processing_speed = 1 / (time.time() - last_time)
            text = ("%0.2f" % (processing_speed,)) + ' fps'
            textsize = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 1, 2)[0]
            cv2.namedWindow(display_name, cv2.WINDOW_AUTOSIZE)
            cv2.putText(display_image, 
                        text,
                        org = (int((display_image.shape[1] - textsize[0]/2)), int((textsize[1])/2)),
                        fontFace = cv2.FONT_HERSHEY_SIMPLEX,
                        fontScale = 0.5,
                        thickness = 1,
                        color = (255, 255, 255))

            # Show the images
            cv2.imshow(display_name, display_image)
            cv2.waitKey(1)

            # Print all the distances in a line
            # print(*distances)
            
            last_time = time.time()

except KeyboardInterrupt:
    send_msg_to_gcs('Closing the script...')  

except Exception as e:
    print(e)
    pass

except:
    send_msg_to_gcs('ERROR: Depth camera disconnected')  

finally:
    pipe.stop()
    vehicle.close()
    print("INFO: Realsense pipe and vehicle object closed.")
    sys.exit()