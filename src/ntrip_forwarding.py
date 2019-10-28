#! /usr/bin/env python
import rospy
from std_msgs.msg import String
import base64
import socket
import errno
import time
import threading
import select
import Queue

# Testing
test_mode = False
dummy_gga = '$GPGGA,134451.797,4250.202,N,08320.949,W,1,12,1.0,0.0,M,0.0,M,,*7D'

# Global variables
rtcm_queue = Queue.Queue()
gga_queue = Queue.Queue()


class NtripSocketThread (threading.Thread):
    def __init__(self, caster_ip, caster_port, mountpoint, username, password):
        threading.Thread.__init__(self)
        self.stop_event = threading.Event()
        self.no_rtcm_data_count = 0
        self.sent_gga = False
        self.ntrip_tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connected_to_caster = False
        self.username = username
        self.password = password
        self.mountpoint = mountpoint
        self.caster_ip = caster_ip
        self.caster_port = caster_port

    def connect_to_ntrip_caster(self):
        print('Connecting to NTRIP caster at %s:%d' % (self.caster_ip, self.caster_port))

        try:
            self.ntrip_tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.ntrip_tcp_sock.settimeout(5.0)
            self.ntrip_tcp_sock.connect((self.caster_ip, self.caster_port))
            self.ntrip_tcp_sock.settimeout(None)
            print('Successfully opened socket')
        except Exception as ex:
            print('Error connecting socket: %s' % ex)
            self.ntrip_tcp_sock.settimeout(None)
            return False

        encoded_credentials = base64.b64encode(self.username + ':' + self.password)
        server_request = 'GET /%s HTTP/1.0\r\nUser-Agent: NTRIP ABC/1.2.3\r\nAccept: */*\r\nConnection: close\r\nAuthorization: Basic %s\r\n\r\n' % (
            self.mountpoint, encoded_credentials)
        self.ntrip_tcp_sock.send(server_request)

        while True:
            try:
                response = self.ntrip_tcp_sock.recv(10000)
            except socket.error as e:
                err = e.args[0]
                if err == errno.EAGAIN or err == errno.EWOULDBLOCK:
                    continue
                else:
                    # a "real" error occurred
                    print(e)
                    return False
            else:
                if 'ICY 200 OK' in response:
                    print('Successfully connected to NTRIP caster')
                    return True
                else:
                    print('Received unexpected response from caster:\n%s' % response)
                    return False

    def run(self):
        print('Starting NTRIP TCP socket thread')
        while not self.stop_event.isSet():

            if not self.connected_to_caster:
                if self.connect_to_ntrip_caster():
                    self.connected_to_caster = True
                else:
                    time.sleep(0.05)
                    continue

            # Receive RTCM messages from NTRIP caster and put in queue to send to GPS receiver
            try:
                ready_to_read, ready_to_write, in_error = select.select([self.ntrip_tcp_sock, ], [self.ntrip_tcp_sock, ], [], 5)
            except select.error:
                self.ntrip_tcp_sock.close()
                self.connected_to_caster = False
                print('Error calling select(): resetting connection to NTRIP caster')
                continue

            if len(ready_to_read) > 0:
                rtcm_msg = self.ntrip_tcp_sock.recv(100000)
                if len(rtcm_msg) > 0:
                    if ord(rtcm_msg[0]) == 0xD3:
                        rtcm_msg_len = 256 * ord(rtcm_msg[1]) + ord(rtcm_msg[2])
                        rtcm_msg_no = (256 * ord(rtcm_msg[3]) + ord(rtcm_msg[4])) / 16
                        print('Received RTCM message %d with length %d' % (rtcm_msg_no, rtcm_msg_len))
                    else:
                        # print('Received ASCII message from server: %s' % str(rtcm_msg))
                        print('%d' % ord(rtcm_msg[0]))

                    rtcm_queue.put(rtcm_msg)
                    self.no_rtcm_data_count = 0

            # Get GPGGA messages from receive queue and send
            # to NTRIP server to keep connection alive
            if len(ready_to_write) > 0:
                try:
                    gga_msg = gga_queue.get_nowait()
                    print('Sending new GGA message to NTRIP caster %s' % gga_msg)
                    self.ntrip_tcp_sock.send(gga_msg)
                    self.sent_gga = True
                except Queue.Empty:
                    pass

            if self.no_rtcm_data_count > 200:
                print('No RTCM messages for 10 seconds; resetting connection to NTRIP caster')
                self.ntrip_tcp_sock.close()
                self.connected_to_caster = False
                self.no_rtcm_data_count = 0

            if self.sent_gga:
                self.no_rtcm_data_count += 1

            time.sleep(0.05)

        print('Stopping NTRIP TCP socket thread')
        self.ntrip_tcp_sock.close()

    def stop(self):
        self.stop_event.set()


class ReceiverThread (threading.Thread):
    def __init__(self, broadcast_port):
        threading.Thread.__init__(self)
        self.stop_event = threading.Event()
        self.receiver_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.receiver_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.broadcast_ip = '195.0.0.255'
        self.broadcast_port = broadcast_port

    def run(self):
        print('Starting relay socket thread')
        while not self.stop_event.isSet():
            # Get RTCM messages from NTRIP TCP socket queue and send to GPS receiver over UDP
            try:
                rtcm_msg = rtcm_queue.get_nowait()
                print('Broadcasting RTCM message to %s:%d' % (self.broadcast_ip, self.broadcast_port))
                self.receiver_sock.sendto(rtcm_msg, (self.broadcast_ip, self.broadcast_port))
            except Queue.Empty:
                # Nothing in the RTCM message queue this time
                pass

            time.sleep(0.05)

    def stop(self):
        self.stop_event.set()


def stop_threads(workers):
    for worker in workers:
        worker.stop()
        worker.join()


def start_threads(caster_ip, caster_port, mountpoint, username, password, broadcast_port):
    workers = [NtripSocketThread(caster_ip, caster_port, mountpoint, username, password), ReceiverThread(broadcast_port)]

    for worker in workers:
        worker.start()
    return workers


class RosInterface:
    def __init__(self):
        rospy.init_node('ntrip_forwarding')

        self.caster_ip = rospy.get_param('~caster_ip', default='')
        self.caster_port = rospy.get_param('~caster_port', default=0)
        self.mountpoint = rospy.get_param('~mountpoint', default='')
        self.username = rospy.get_param('~ntrip_username', default='')
        self.password = rospy.get_param('~ntrip_password', default='')

        self.broadcast_port = rospy.get_param('~broadcast_port', default=0)

        self.gga_timer = rospy.Timer(rospy.Duration(5.0), self.gga_timer_cb)
        rospy.Subscriber('gps/gga', String, self.recv_gga)

        self.gga_msg = ''
        self.workers = start_threads(self.caster_ip, self.caster_port, self.mountpoint, self.username, self.password, self.broadcast_port)

    def recv_gga(self, msg):
        self.gga_msg = msg.data

    def gga_timer_cb(self, event):
        if test_mode:
            gga_queue.put(dummy_gga)
        else:
            if len(self.gga_msg) > 0:
                gga_queue.put(self.gga_msg)

    def on_shutdown(self):
        print('Shutting down')
        stop_threads(self.workers)


if __name__ == '__main__':

    ros_interface = RosInterface()
    rospy.on_shutdown(ros_interface.on_shutdown)

    rospy.spin()
