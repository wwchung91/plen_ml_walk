#!/usr/bin/env python

import rospy
import gym
from gym.utils import seeding
from plen_ros_helpers.gazebo_connection import GazeboConnection
from plen_ros_helpers.controllers_connection import ControllersConnection
from openai_ros.msg import RLExperimentInfo
from plen_ros.srv import Iterate
import numpy as np
import time

from rosgraph_msgs.msg import Clock

#https://bitbucket.org/theconstructcore/theconstruct_msgs/src/master/msg/RLExperimentInfo.msg
# from openai_ros.msg import RLExperimentInfo - CHANGE THIS TO NORMAL MESSAGE IMPORT


# https://github.com/openai/gym/blob/master/gym/core.py
class RobotGazeboEnv(gym.Env):
    def __init__(self,
                 robot_name_space,
                 controllers_list,
                 reset_controls,
                 start_init_physics_parameters=True,
                 reset_world_or_sim="SIMULATION"):

        # To reset Simulations
        rospy.logdebug("START init RobotGazeboEnv")
        self.gazebo = GazeboConnection(start_init_physics_parameters,
                                       reset_world_or_sim)

        self.gazebo_sim = GazeboConnection(start_init_physics_parameters,
                                           "SIMULATION")
        self.controllers_object = ControllersConnection(
            namespace=robot_name_space, controllers_list=controllers_list)
        self.reset_controls = reset_controls
        self.controllers_list = controllers_list
        self.seed()
        self.robot_name_space = robot_name_space

        # GAZEBO CLOCK SUBSCRIBER - SUPER IMPORTANT FOR DETERMINISTIC STEPS
        self.clock_subscriber = rospy.Subscriber(
            '/clock', Clock,
            self.clock_callback)

        # /iterate SERVICE CLIENT, ALSO IMPORTANT FOR DETERMINISTIC STEPS
        self.iterate_proxy = rospy.ServiceProxy(
            '/iterate', Iterate)

        # Set up ROS related variables
        self.episode_num = 0
        self.cumulated_episode_reward = 0
        self.episode_timestep = 0
        self.total_timesteps = 0
        # Set up values for moving average pub
        self.moving_avg_buffer_size = 1000
        self.moving_avg_buffer = np.zeros(self.moving_avg_buffer_size)
        self.moving_avg_counter = 0
        self.reward_pub = rospy.Publisher('/' + self.robot_name_space +
                                          '/reward',
                                          RLExperimentInfo,
                                          queue_size=1)

        # We Unpause the simulation and reset the controllers if needed
        """
        To check any topic we need to have the simulations running, we need to do two things:
        1) Unpause the simulation: without that th stream of data doesnt flow. This is for simulations
        that are pause for whatever the reason
        2) If the simulation was running already for some reason, we need to reset the controlers.
        This has to do with the fact that some plugins with tf, dont understand the reset of the simulation
        and need to be reseted to work properly.
        """
        self.gazebo.unpauseSim()
        self.controllers_object.reset_controllers()

        self._check_all_systems_ready()

        self.gazebo.pauseSim()

        rospy.logdebug("END init RobotGazeboEnv")

    def clock_callback(self, data):
        """
        """
        self.sim_time = data.clock

    # Env methods
    def seed(self, seed=None):
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        """
        Function executed each time step.
        Here we get the action execute it in a time step and retrieve the
        observations generated by that action.
        :param action:
        :return: obs, reward, done, info
        """
        """
        Here we should convert the action num to movement action, execute the action in the
        simulation and get the observations result of performing that action.
        """
        # rospy.logdebug("START STEP OpenAIROS")
        self._set_action(action)  # handles unpause and pause
        # time.sleep(2)  # FOR TESTING
        obs = self._get_obs()
        done = self._is_done(obs)
        info = {}
        reward = self._compute_reward(obs, done)
        self.cumulated_episode_reward += reward
        self.episode_timestep += 1
        self.total_timesteps += 1

        # rospy.loginfo("STEP DONE")

        return obs, reward, done, info

    def reset(self):
        # rospy.logdebug("Reseting RobotGazeboEnvironment")
        self._reset_sim()
        self._init_env_variables()
        self._update_episode()
        obs = self._get_obs()
        # rospy.logdebug("END Reseting RobotGazeboEnvironment")
        return obs

    def close(self):
        """
        Function executed when closing the environment.
        Use it for closing GUIS and other systems that need closing.
        :return:
        """
        rospy.logerr("Closing RobotGazeboEnvironment")
        rospy.signal_shutdown("Closing RobotGazeboEnvironment")

    def _update_episode(self):
        """
        Publishes the cumulated reward of the episode and
        increases the episode number by one.
        :return:
        """
        # rospy.logdebug("PUBLISHING REWARD...")
        self._publish_reward_topic(self.cumulated_episode_reward,
                                   self.episode_num)
        # rospy.logdebug("EPISODE REWARD = " +
        #                str(self.cumulated_episode_reward) + ", EP = " +
        #                str(self.episode_num))

        self.episode_num += 1
        self.moving_avg_counter += 1

        self.cumulated_episode_reward = 0
        self.episode_timestep = 0

    def _publish_reward_topic(self, reward, episode_number=1):
        """
        This function publishes the given reward in the reward topic for
        easy access from ROS infrastructure.
        :param reward:
        :param episode_number:
        :return:
        """
        reward_msg = RLExperimentInfo()
        reward_msg.episode_number = episode_number
        reward_msg.total_timesteps = self.total_timesteps
        reward_msg.episode_reward = reward

        # Now Calculate Moving Avg
        if self.moving_avg_counter >= self.moving_avg_buffer_size:
            self.moving_avg_counter = 0
        self.moving_avg_buffer[
            self.moving_avg_counter] = self.cumulated_episode_reward
        # Only publish moving avg if enough samples
        if self.episode_num >= self.moving_avg_buffer_size:
            reward_msg.moving_avg_reward = np.average(self.moving_avg_buffer)
        else:
            reward_msg.moving_avg_reward = np.nan
        self.reward_pub.publish(reward_msg)

        # Sometimes after 9999+ resets, gazebo has problems.
        # so we try a simulation reset
        if np.isnan(reward):
            rospy.logerr("---------------------------------------------------")
            rospy.logerr("CLOSING SIM")
            self.close()
            # if self.reset_controls:
            #     rospy.logdebug("RESET CONTROLLERS")
            #     self.gazebo.unpauseSim()
            #     self.controllers_object.reset_controllers()
            #     self._check_all_systems_ready()
            #     self._set_init_pose()
            #     rospy.sleep(0.5)
            #     self.gazebo.pauseSim()
            #     # Reset Sim to try and remove nan
            #     self.gazebo_sim.resetSim()
            #     self.gazebo.unpauseSim()
            #     self.controllers_object.reset_controllers()
            #     self._check_all_systems_ready()
            #     self.gazebo.pauseSim()

    # Extension methods
    # ----------------------------

    def _reset_sim(self):
        """Resets a simulation
        """
        rospy.logdebug(
            "------------------------------------------------------")
        rospy.logdebug("RESETTING")
        if self.reset_controls:
            rospy.logdebug("RESET CONTROLLERS")
            self.gazebo.unpauseSim()
            self.controllers_object.reset_controllers()
            self._check_all_systems_ready()
            # self.gazebo.pauseSim()
            self._set_init_pose()  # handles unpause and pause
            self.gazebo.pauseSim()
            self.gazebo.resetSim()
            self.gazebo.unpauseSim()
            self.controllers_object.reset_controllers()
            self._check_all_systems_ready()
            self.gazebo.reset_joints(self.controllers_list, "plen")
            self.gazebo.pauseSim()
            """
            rospy.logdebug("RESET CONTROLLERS")
            self.gazebo.unpauseSim()
            self.controllers_object.switch_controllers(
                controllers_on=[],
                controllers_off=self.controllers_object.controllers_list)
            self.gazebo.pauseSim()
            self.gazebo.reset_joints(self.controllers_object.controllers_list,
                                     self.robot_name_space)
            self.gazebo.unpauseSim()
            self._check_all_systems_ready()
            self.gazebo.pauseSim()
            self.gazebo.resetSim()
            self.gazebo.unpauseSim()
            self.controllers_object.switch_controllers(
                controllers_on=self.controllers_object.controllers_list,
                controllers_off=[])
            self._check_all_systems_ready()
            self._set_init_pose()
            self.gazebo.pauseSim()
            """

        else:
            rospy.logdebug("DONT RESET CONTROLLERS")
            # Unpause
            self.gazebo.unpauseSim()
            # Check Controllers/Sensors
            self._check_all_systems_ready()
            # Set Joints to Init
            self._set_init_pose()
            # rospy.sleep(0.5)
            # Pause
            self.gazebo.pauseSim()
            # Reset Pose or Sim (see input to GazeboConnection)
            self.gazebo.resetSim()
            # Unpause
            self.gazebo.unpauseSim()
            # Check Controllers/Sensors
            self._check_all_systems_ready()
            # Pause
            self.gazebo.pauseSim()

        rospy.logdebug("RESET SIM END")
        return True

    def _set_init_pose(self):
        """Sets the Robot in its init pose
        """
        raise NotImplementedError()

    def _check_all_systems_ready(self):
        """
        Checks that all the sensors, publishers and other simulation systems are
        operational.
        """
        raise NotImplementedError()

    def _get_obs(self):
        """Returns the observation.
        """
        raise NotImplementedError()

    def _init_env_variables(self):
        """Inits variables needed to be initialised each time we reset at the start
        of an episode.
        """
        raise NotImplementedError()

    def _set_action(self, action):
        """Applies the given action to the simulation.
        """
        raise NotImplementedError()

    def _is_done(self, observations):
        """Indicates whether or not the episode is done ( the robot has fallen for example).
        """
        raise NotImplementedError()

    def _compute_reward(self, observations, done):
        """Calculates the reward to give based on the observations given.
        """
        raise NotImplementedError()

    def _env_setup(self, initial_qpos):
        """Initial configuration of the environment. Can be used to configure initial state
        and extract information from the simulation.
        """
        raise NotImplementedError()
