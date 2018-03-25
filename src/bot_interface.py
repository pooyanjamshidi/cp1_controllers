#! /usr/bin/env python

import traceback
import math
import json
import numpy
from threading import Lock
import os

# third party imports
import rospy
from std_msgs.msg import Float64
from actionlib_msgs.msg import *
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from geometry_msgs.msg import *
from gazebo_msgs.msg import *
from gazebo_msgs.srv import *
import actionlib
import dynamic_reconfigure.client
from tf.transformations import euler_from_quaternion, quaternion_from_euler
import ig_action_msgs.msg
from ig_action_msgs.msg import InstructionGraphResult
from mars_notifications.msg import UserNotification
from kobuki_msgs.msg import MotorPower

# importing battery services
from brass_gazebo_battery.srv import *
# importing configuration manager services
from brass_gazebo_config_manager.srv import *

# parameters and global variables
ros_node = '/battery_monitor_client'
model_name = '/battery_demo_model'
map_name = 'map'
max_waiting_time = 100

# the threshold below which the bot will go to the charging station
battery_low_threshold = 0.90

conf_file = '../conf/conf.json'

default_configuration_id = 0
default_power_load = 10

# This is the model for the obstacle
obstacle = os.path.expanduser('~/catkin_ws/src/cp1_base/models/box')


# Here we manage the world, bot, and control interface

def status_translator(status):
    if 0: print ''
    elif status == GoalStatus.PENDING   : state='PENDING'
    elif status == GoalStatus.ACTIVE    : state='ACTIVE'
    elif status == GoalStatus.PREEMPTED : state='PREEMPTED'
    elif status == GoalStatus.SUCCEEDED : state='SUCCEEDED'
    elif status == GoalStatus.ABORTED   : state='ABORTED'
    elif status == GoalStatus.REJECTED  : state='REJECTED'
    elif status == GoalStatus.PREEMPTING: state='PREEMPTING'
    elif status == GoalStatus.RECALLING : state='RECALLING'
    elif status == GoalStatus.RECALLED  : state='RECALLED'
    elif status == GoalStatus.LOST      : state='LOST'
    return 'Status({} - {})'.format(status, state)


class ControlInterface:

    def __init__(self):

        # standard Gazebo services
        self.get_model_state = rospy.ServiceProxy('/gazebo/get_model_state', GetModelState)
        self.set_model_state = rospy.ServiceProxy('/gazebo/set_model_state', SetModelState)

        self.spawn_model = rospy.ServiceProxy('/gazebo/spawn_sdf_model', SpawnModel)
        self.delete_model = rospy.ServiceProxy('/gazebo/delete_model', DeleteModel)

        # Battery plugin Gazebo services
        self.set_charging_srv = rospy.ServiceProxy(ros_node + model_name + '/set_charging', SetCharging)
        self.set_charge_rate_srv = rospy.ServiceProxy(ros_node + model_name + '/set_charge_rate', SetChargingRate)
        self.set_charge_srv = rospy.ServiceProxy(ros_node + model_name + '/set_charge', SetCharge)
        self.set_powerload_srv = rospy.ServiceProxy(ros_node + model_name + '/set_power_load', SetLoad)
        self.get_configuration_srv = rospy.ServiceProxy(ros_node + model_name + '/get_robot_configuration', GetConfig)

        # AMCL topic
        self.amcl = rospy.Publisher('initialpose', PoseWithCovarianceStamped, queue_size=10, latch=True)

        self.battery_charge = -1
        self.battery_capacity = 1.2009
        self.is_charging = False
        self.is_battery_low = False

        self.movebase_client = None
        self.ig_client = None
        self.ig_server = None

        self.bot_conf = None

        obstacle_xml_file = open(obstacle + '.sdf')
        self.obs_xml = obstacle_xml_file.read()

        self.obstacles = []
        self.obstacle_seq = 0
        self.lock = Lock()

        self.battery_previous_update = self.battery_charge

        # default configuration is zero id
        self.current_config = 0

    def read_conf(self):

        with open(conf_file) as json_file:
            self.bot_conf = json.load(json_file)

    def update_conf(self, conf):

        with open(conf_file, 'w') as json_file:
            json.dump(conf, json_file)

        # update the bot configuration
        self.bot_conf = conf

    def connect_to_navigation_server(self):

        self.movebase_client = actionlib.SimpleActionClient("move_base", MoveBaseAction)

        while not self.movebase_client.wait_for_server(rospy.Duration.from_sec(5)):
            rospy.loginfo("waiting for the action server")

        rospy.loginfo("successfully connected to the action server")
        return True

    def move_to_point(self, x, y):

        goal = MoveBaseGoal()

        goal.target_pose.header.frame_id = map_name
        goal.target_pose.header.stamp = rospy.Time.now()

        goal.target_pose.pose.position = Point(x, y, 0)

        goal.target_pose.pose.orientation.x = 0.0
        goal.target_pose.pose.orientation.y = 0.0
        goal.target_pose.pose.orientation.z = 0.0
        goal.target_pose.pose.orientation.w = 1.0

        self.movebase_client.send_goal(goal)
        success = self.movebase_client.wait_for_result(rospy.Duration.from_sec(max_waiting_time))

        state = self.movebase_client.get_state()

        if success and state == GoalStatus.SUCCEEDED:
            rospy.loginfo("reached the destination")
            return True
        else:
            rospy.loginfo("could not reached the destination")
            return False

    def connect_to_ig_action_server(self):

        # self.ig_server = actionlib.SimpleActionServer("ig_action_server", ig_action_msgs.msg.InstructionGraphAction)
        self.ig_client = actionlib.SimpleActionClient("ig_action_server", ig_action_msgs.msg.InstructionGraphAction)

        while not self.ig_client.wait_for_server(rospy.Duration.from_sec(max_waiting_time)):
            rospy.logwarn("waiting for the ig_action_server")

        rospy.loginfo("successfully connected to the ig_action_server")
        return True

    def move_bot_with_ig(self, ig_file):

        with open(ig_file) as igfile:
            igcode = igfile.read()
            goal = ig_action_msgs.msg.InstructionGraphGoal(order=igcode)
            self.ig_client.send_goal(goal=goal)
            success = self.ig_client.wait_for_result(rospy.Duration.from_sec(max_waiting_time))

            state = self.ig_client.get_state()
            if success and state == GoalStatus.SUCCEEDED:
                rospy.loginfo("Successfully executed the instructions and reached the destination")
                return True
            else:
                rospy.loginfo("could not execute the instructions")
                return False

    def move_bot_with_igcode(self, igcode):

        goal = ig_action_msgs.msg.InstructionGraphGoal(order=igcode)
        self.ig_client.send_goal(goal=goal, done_cb=self.done_cb, active_cb=self.active_cb, feedback_cb=self.feedback_cb)
        success = self.ig_client.wait_for_result(rospy.Duration.from_sec(max_waiting_time))

        state = self.ig_client.get_state()

        if success and state == GoalStatus.SUCCEEDED:
            rospy.loginfo("Successfully executed the instructions and reached the destination")
            return True
        else:
            rospy.loginfo("could not execute the instructions")
            return False

    def set_bot_position(self, x, y, w):

        try:
            tp = self.get_model_state('mobile_base', '')

            tp.pose.position.x = x
            tp.pose.position.y = y
            quat = (tp.pose.orientation.x, tp.pose.orientation.y, tp.pose.orientation.z, tp.pose.orientation.w)
            (roll, pitch, yaw) = euler_from_quaternion(quat)
            yaw = w
            quat = quaternion_from_euler(roll, pitch, yaw)

            tp.pose.orientation.x = quat[0]
            tp.pose.orientation.y = quat[1]
            tp.pose.orientation.z = quat[2]
            tp.pose.orientation.w = quat[3]

            ms = ModelState()
            ms.model_name = "mobile_base"
            ms.pose = tp.pose
            ms.twist = tp.twist

            result = self.set_model_state(ms)

            if result.success:
                ip = PoseWithCovarianceStamped()
                ip.header.stamp = rospy.Time.now()
                ip.header.frame_id = map_name
                ip.pose.pose.position.x = x
                ip.pose.pose.position.y = y
                ip.pose.pose.position.z = 0
                ip.pose.pose.orientation.x = tp.pose.orientation.x
                ip.pose.pose.orientation.y = tp.pose.orientation.y
                ip.pose.pose.orientation.z = tp.pose.orientation.z
                ip.pose.pose.orientation.w = tp.pose.orientation.w
                self.amcl.publish(ip)
                rospy.loginfo("The bot is positioned in the new place at ({0}, {1})".format(x, y))
                return True
            else:
                rospy.logerr("Error occurred putting the bot in the position")
                return False

        except rospy.ServiceException as e:
            rospy.logerr("Could not set the position of the bot")
            rospy.logerr(e.message)

    def get_bot_state(self):

        try:
            tp = self.get_model_state('mobile_base', '')
            quat = (tp.pose.orientation.x, tp.pose.orientation.y, tp.pose.orientation.z, tp.pose.orientation.w)
            (roll, pitch, yaw) = euler_from_quaternion(quat)
            v = math.sqrt(tp.twist.linear.x**2 + tp.twist.linear.y**2)
            return tp.pose.position.x, tp.pose.position.y, yaw, v

        except rospy.ServiceException as se:
            rospy.logerr("Error happened while getting bot position: %s", se)
            return None, None, None, None

    def get_current_configuration(self, current_or_historical):
        self.current_config = self.get_configuration_srv(current_or_historical)
        return self.current_config

    def set_charging(self, charging):
        self.is_charging = charging
        return self.set_charging_srv(charging)

    def set_charge(self, charge):
        return self.set_charge_srv(charge)

    def set_power_load(self, load):
        return self.set_powerload_srv(load)

    def set_charging_rate(self, charge_rate):
        return self.set_charge_rate_srv(charge_rate)

    def get_charge(self, msg):
        self.battery_charge = msg.data
        #  determine whether the battery is low or not
        if self.battery_charge < battery_low_threshold:
            self.is_battery_low = True
        else:
            self.is_battery_low = False

        if abs(self.battery_charge - self.battery_previous_update) > self.battery_capacity*0.01:
            rospy.loginfo("Battery charge: {0}Ah".format(self.battery_charge))
            self.battery_previous_update = self.battery_charge

    def monitor_battery(self):
        # rospy.init_node("battery_monitor_client")
        rospy.Subscriber("/mobile_base/commands/charge_level", Float64, self.get_charge)
        rospy.spin()

    def track_battery_charge(self):
        """starts monitoring battery and update battery_charge"""
        # rospy.init_node("battery_monitor_client")
        rospy.Subscriber("/mobile_base/commands/charge_level", Float64, self.get_charge)
        return self.get_charge

    def active_cb(self):
        rospy.loginfo("Plan is active!")

    def done_cb(self, status, result):
        if status == GoalStatus.SUCCEEDED:
            rospy.loginfo("done_cb: Task succeeded!")
        else:
            rospy.logwarn("done_cb: Unhandled Action response: {}".format(status_translator(status)))

    def feedback_cb(self, feedback):
        # first get the latest charge and then determine whether the bot should abort the task
        if self.battery_charge < battery_low_threshold * self.battery_capacity:
            # self.ig_client.cancel_goal()
            self.is_battery_low = True
            rospy.logwarn("Battery level is low and the goal has been cancelled to send the robot to charge station")
        else:
            self.is_battery_low = False
            rospy.loginfo("Battery level is OK")

    def place_obstacle(self, x, y):
        """similar to phase 1"""

        pose = Pose()
        zero_q = quaternion_from_euler(0, 0, 0)
        pose.position.x = x
        pose.position.y = y
        pose.position.z = 0
        pose.orientation.x = zero_q[0]
        pose.orientation.y = zero_q[1]
        pose.orientation.z = zero_q[2]
        pose.orientation.w = zero_q[3]

        with self.lock:
            obstacle_name = 'Obstacle_{0}'.format(self.obstacle_seq)

        req = SpawnModelRequest()
        req.model_name = obstacle_name
        req.initial_pose = pose
        req.model_xml = self.obs_xml

        try:
            res = self.spawn_model(req)
            if res.success:
                with self.lock:
                    self.obstacle_seq += 1
                    self.obstacles.append(obstacle_name)
                return obstacle_name
            else:
                rospy.logerr("Could not place obstacle. Message: {0}".format(res.status_message))
                return None
        except rospy.ServiceException as e:
            rospy.logerr("Could not place obstacle. Message {0}".format(e))
            return None

    def remove_obstacle(self, obstacle_name):
        """similar to phase 1"""

        with self.lock:
            if obstacle_name not in self.obstacles:
                rospy.logerr('The obstacle could not find in the world: {0}'.format(obstacle_name))
                return False

        req = DeleteModelRequest()
        req.model_name = obstacle_name
        try:
            res = self.delete_model(req)

            if res.success:
                with self.lock:
                    self.obstacles.remove(obstacle_name)

                return True
            else:
                rospy.logerr("Could not remove obstacle. Message: {0}".format(res.status_message))
                return None
        except rospy.ServiceException as e:
            rospy.logerr("Could not place obstacle. Message {0}".format(e))
            return None


def main():

    global battery_charge
    battery_charge = -1

    rospy.init_node('navigation', anonymous=False)

    ci = ControlInterface()
    state = ci.get_bot_state()
    print("Bot is located at ({0}, {1}), facing {2} and going with a speed of {3} m/s".format(state[0], state[1], state[2], state[3]))

    ci.set_power_load(1)
    ci.set_charge(1)
    ci.set_charging(1)

    # monitor_battery()
    # ci.move_to_point(0, -1)
    # ci.set_bot_position(0, 0, 0)

    ci.read_conf()

    ci.connect_to_ig_action_server()
    ci.move_bot_with_ig('../instructions/nav_test1.ig')


if __name__ == '__main__':
    main()
