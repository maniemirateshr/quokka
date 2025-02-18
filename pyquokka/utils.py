import os
import time
import boto3
import subprocess
import multiprocessing
import pyquokka
import ray
import json
import signal
import polars
from pssh.clients import ParallelSSHClient

def preexec_function():
    # Ignore the SIGINT signal by setting the handler to the standard
    # signal handler SIG_IGN.
    signal.signal(signal.SIGINT, signal.SIG_IGN)

class EC2Cluster:
    def __init__(self, public_ips, private_ips, instance_ids, cpu_count_per_instance, spill_dir) -> None:

        """
        Not meant to be called directly. Use QuokkaClusterManager to create a cluster.
        """
        
        self.num_node = len(public_ips)
        self.public_ips = {}
        self.private_ips = {}
        self.instance_ids = {}
        self.spill_dir = spill_dir

        for node in range(self.num_node):
            self.public_ips[node] = public_ips[node]
            self.private_ips[node] = private_ips[node]
            self.instance_ids[node] = instance_ids[node]
        
        self.state = "running"
        self.cpu_count = cpu_count_per_instance
        self.leader_public_ip = self.public_ips[0]
        self.leader_private_ip = self.private_ips[0]
        print("EC2 Cluster leader public IP", self.leader_public_ip, "private IP", self.leader_private_ip)
        pyquokka_loc = pyquokka.__file__.replace("__init__.py","")
        # connect to that ray cluster
        ray.init(address='ray://' + str(self.leader_public_ip) + ':10001', 
                 runtime_env={"py_modules":[pyquokka_loc]})
    
    def to_json(self, output = "cluster.json"):

        """
        Creates JSON representation of this cluster that can be used to connect to the cluster again.

        Args:
            output (str, optional): Path to output file. Defaults to "cluster.json".

        Return:
            None

        Examples:

            >>> from pyquokka.utils import *
            >>> manager =  QuokkaClusterManager(key_name = "my_key", key_location = "/home/ubuntu/.ssh/my_key.pem", security_group = "my_security_group")
            >>> cluster = manager.create_cluster(aws_access_key, aws_access_id, num_instances = 2, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = ["numpy", "pandas"])
            >>> cluster.to_json("my_cluster.json")

            You can now close this Python session. In a new Python session you can connect to this cluster by doing:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager(key_name = "my_key", key_location = "/home/ubuntu/.ssh/my_key.pem", security_group = "my_security_group")
            >>> cluster = manager.from_json("my_cluster.json")
        
        """

        json.dump({"instance_ids":self.instance_ids,"cpu_count_per_instance":self.cpu_count, "spill_dir": self.spill_dir},open(output,"w"))


class LocalCluster:
    def __init__(self) -> None:

        """
        Creates a local cluster on your machine. This is useful for testing purposes. This not should be necessary because `QuokkaContext` will automatically
        make one for you.

        Return:
            LocalCluster: A LocalCluster object.

        Examples:

            >>> from pyquokka.utils import *
            >>> cluster = LocalCluster()
            >>> from pyquokka.df import QuokkaContext
            >>> qc = QuokkaContext(cluster)

            But the following is equivalent:

            >>> from pyquokka.df import QuokkaContext
            >>> qc = QuokkaContext()
            
        """

        print("Initializing local Quokka cluster.")
        self.num_node = 1
        self.cpu_count = multiprocessing.cpu_count()
        pyquokka_loc = pyquokka.__file__.replace("__init__.py","")
        # we assume you have pyquokka installed, and we are going to spin up a ray cluster locally
        ray.init(ignore_reinit_error=True)
        flight_file = pyquokka_loc + "/flight.py"
        self.flight_process = None
        self.redis_process = None
        os.system("export GLIBC_TUNABLES=glibc.malloc.trim_threshold=524288")
        port5005 = os.popen("lsof -i:5005").read()
        if "python" in port5005:
            raise Exception("Port 5005 is already in use. Kill the process that is using it first.")
            
        try:
            self.flight_process = subprocess.Popen(["python3", flight_file], preexec_fn = preexec_function)
        except:
            raise Exception("Could not start flight server properly. Check if there is already something using port 5005, kill it if necessary. Use lsof -i:5005")
        self.redis_process = subprocess.Popen(["redis-server" , pyquokka_loc + "redis.conf", "--port 6800", "--protected-mode no"], preexec_fn=preexec_function)
        self.leader_public_ip = "localhost"
        self.leader_private_ip = ray.get_runtime_context().gcs_address.split(":")[0]
        self.public_ips = {0:"localhost"}
        self.private_ips = {0: ray.get_runtime_context().gcs_address.split(":")[0]}
        print("Finished setting up local Quokka cluster.")
    
    def __del__(self):
        # we need to join the process that is running the flight server! 
        if self.flight_process is not None:
            self.flight_process.kill()
        if self.redis_process is not None:
            self.redis_process.kill()


def execute_script(key_location, x):
    return os.system("ssh -oStrictHostKeyChecking=no -i {} ubuntu@{} 'bash -s' < {}".format(key_location, x, pyquokka.__file__.replace("__init__.py", "common_startup.sh")))

class QuokkaClusterManager:

    def __init__(self, key_name = None, key_location = None, security_group= None) -> None:
        
        """
        Create a QuokkaClusterManager object. This object is used to create Ray clusters on AWS EC2 configured with Quokka or connecting to existing Ray clusters.
        This requires you to have an AWS key pair for logging into instances. 

        Args:
            key_name (str, optional): The name of the key pair to use. This is a required argument if you want to call `create_cluster`.
            key_location (str, optional): The location of the key pair to use. You must specify this argument.
            security_group (str, optional): The security group to use. 
        
        Return:
            QuokkaClusterManager: A QuokkaClusterManager object.
        
        Examples:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager(key_name = "my_key", key_location = "/home/ubuntu/.ssh/my_key.pem", security_group = "my_security_group")
            >>> cluster = manager.create_cluster(aws_access_key, aws_access_id, num_instances = 2, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = ["numpy", "pandas"])
            >>> cluster.to_json("my_cluster.json")

            You can now close this Python session. In a new Python session you can connect to this cluster by doing:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager(key_name = "my_key", key_location = "/home/ubuntu/.ssh/my_key.pem", security_group = "my_security_group")
            >>> cluster = manager.from_json("my_cluster.json")
        """

        assert key_location is not None
        self.key_name = key_name
        self.key_location = key_location
        self.security_group = security_group

    def str_key_to_int(self, d):
        return {int(i):d[i] for i in d}
    
    def install_python_package(self, cluster, req):
        assert type(cluster) == EC2Cluster
        self.launch_all("pip3 install " + req, list(cluster.public_ips.values()), "Failed to install " + req)

    def launch_all(self, command, ips, error = "Error", ignore_error = False):

        client = ParallelSSHClient(ips, user="ubuntu", pkey=self.key_location, timeout=5)
        output = client.run_command(command)
        result = []
        for host_output in output:
            for line in host_output.stdout:
                result.append(line)
            exit_code = host_output.exit_code
            assert exit_code == 0 or ignore_error, exit_code
        return result

    def copy_all(self, file_path, ips, error = "Error"):
        commands = ["scp -oStrictHostKeyChecking=no -oConnectTimeout=2 -i " + self.key_location + " " + file_path + " ubuntu@" + str(ip) + ":. " for ip in ips]
        processes = [subprocess.Popen(command, close_fds=True, shell=True) for command in commands]
        return_codes = [process.wait() for process in processes]
        if sum(return_codes) != 0:
            raise Exception(error)

    def check_instance_alive(self, public_ips):
        count = 0
        while True:
            z = [os.system("ssh -oStrictHostKeyChecking=no -oConnectTimeout=2 -i " + self.key_location + " ubuntu@" + public_ip +" time") for public_ip in public_ips]
            if sum(z) == 0:
                break
            else:
                count += 1
                if count == 6:
                    raise Exception("Couldn't connect to new instance in 30 seconds.")
                time.sleep(5)
    
    def _initialize_instances(self, instance_ids, spill_dir):
        ec2 = boto3.client("ec2")
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=instance_ids)
        a = ec2.describe_instances(InstanceIds = instance_ids)
        public_ips = [k['PublicIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 
        private_ips = [k['PrivateIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 
        
        leader_public_ip = public_ips[0]
        leader_private_ip = private_ips[0]

        self.check_instance_alive(public_ips)

        self.set_up_spill_dir(public_ips, spill_dir)
        z = os.system("ssh -oStrictHostKeyChecking=no -i " + self.key_location + " ubuntu@" + leader_public_ip + " 'bash -s' < " + pyquokka.__file__.replace("__init__.py","leader_startup.sh"))
        print(z)
        z = os.system("ssh -oStrictHostKeyChecking=no -i " + self.key_location + " ubuntu@" + leader_public_ip + " 'bash -s' < " + pyquokka.__file__.replace("__init__.py","leader_start_ray.sh"))
        print(z)

        command ="/home/ubuntu/.local/bin/ray start --address='" + str(leader_private_ip) + ":6380' --redis-password='5241590000000000'"
        self.launch_all(command, public_ips, "ray workers failed to connect to ray head node")

        self.copy_and_launch_flight(public_ips)

    def set_up_envs(self, public_ips, requirements, aws_access_key, aws_access_id):
            
        import multiprocessing
        pool = multiprocessing.Pool(multiprocessing.cpu_count())        
        pool.starmap(execute_script, [(self.key_location, public_ip) for public_ip in public_ips])

        self.launch_all("aws configure set aws_secret_access_key " + str(aws_access_key), public_ips, "Failed to set AWS access key")
        self.launch_all("aws configure set aws_access_key_id " + str(aws_access_id), public_ips, "Failed to set AWS access id")

        # cluster must have same ray version as client.
        requirements = ["ray==" + ray.__version__, "polars==" + polars.__version__,  "pyquokka"] + requirements
        for req in requirements:
            assert type(req) == str
            try:
                self.launch_all("pip3 install " + req, public_ips, "Failed to install " + req)
            except:
                pass

    def copy_and_launch_flight(self, public_ips):
        
        self.copy_all(pyquokka.__file__.replace("__init__.py","flight.py"), public_ips, "Failed to copy flight server file.")
        self.launch_all("export GLIBC_TUNABLES=glibc.malloc.trim_threshold=524288", public_ips, "Failed to set malloc limit")
        self.launch_all("nohup python3 -u flight.py > foo.out 2> foo.err < /dev/null &", public_ips, "Failed to start flight servers on workers.")

    def set_up_spill_dir(self, public_ips, spill_dir):
        print("Trying to set up spill dir.")   
        result = self.launch_all("sudo nvme list", public_ips, "failed to list nvme devices")
        devices = [sentence.split(" ")[0] for sentence in result if "Amazon EC2 NVMe Instance Storage" in sentence]
        if len(devices) == 0:
            print("No nvme devices found. Skipping.")
            return
        assert all([device == devices[0] for device in devices]), "All instances must have same nvme device location. Raise Github issue if you see this."
        device = devices[0]
        print("Found nvme device: ", device)
        
        try:
            self.launch_all("sudo mkfs.ext4 -F -E nodiscard {};".format(device), public_ips, "failed to format nvme ssd")
            self.launch_all("sudo mount {} {};".format(device, spill_dir), public_ips, "failed to mount nvme ssd")
            self.launch_all("sudo chmod -R a+rw {}".format(spill_dir), public_ips, "failed to give spill dir permissions")
        except:
            pass

    def create_cluster(self, aws_access_key, aws_access_id, num_instances, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = [], spill_dir = "/data"):

        """
        Create a Ray cluster configured to run Quokka applications.

        Args:
            aws_access_key (str): AWS access key.
            aws_access_id (str): AWS access id.
            num_instances (int): Number of instances to create.
            instance_type (str): Instance type to use, defaults to i3.2xlarge.
            ami (str): AMI to use, defaults to "ami-0530ca8899fac469f", which is us-west-2 ubuntu 20.04. Please change accordingly for your region and OS.
            requirements (list): List of requirements to install on cluster, defaults to empty list.
            spill_dir (str): Directory to use for spill directory, defaults to "/data". Quokka will detect if your instance have NVME SSD and mount it to this directory.

        Return:
            EC2Cluster object. See EC2Cluster for more details.
        
        Examples:

            >>> from pyquokka.utils import * 
            >>> manager = QuokkaClusterManager()
            >>> cluster = manager.create_cluster(aws_access_key, aws_access_id, num_instances = 2, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = ["numpy", "pandas"])
            >>> cluster.to_json("my_cluster.json")
            >>> from pyquokka.df import QuokkaContext
            >>> qc = QuokkaContext(cluster)
            >>> df = qc.read_csv("s3://my_bucket/my_file.csv")
                    
        """

        start_time = time.time()
        ec2 = boto3.client("ec2")
        vcpu_per_node = ec2.describe_instance_types(InstanceTypes=[instance_type])['InstanceTypes'][0]['VCpuInfo']['DefaultVCpus']
        waiter = ec2.get_waiter('instance_running')
        res = ec2.run_instances(ImageId=ami, InstanceType = instance_type, SecurityGroupIds = [self.security_group], KeyName=self.key_name ,MaxCount=num_instances, MinCount=num_instances)
        instance_ids = [res['Instances'][i]['InstanceId'] for i in range(num_instances)] 
        waiter.wait(InstanceIds=instance_ids)
        a = ec2.describe_instances(InstanceIds = instance_ids)
        public_ips = [a['Reservations'][0]['Instances'][i]['PublicIpAddress'] for i in range(num_instances)]
        private_ips = [a['Reservations'][0]['Instances'][i]['PrivateIpAddress'] for i in range(num_instances)]

        self.check_instance_alive(public_ips)

        self.set_up_envs(public_ips, requirements, aws_access_key, aws_access_id)
        self.launch_all("sudo mkdir {}".format(spill_dir), public_ips, "failed to make temp spill directory")
        self._initialize_instances(instance_ids, spill_dir)

        print("Launching of Quokka cluster used: ", time.time() - start_time)

        return EC2Cluster(public_ips, private_ips, instance_ids, vcpu_per_node, spill_dir)  
        

    def stop_cluster(self, quokka_cluster):

        """
        Stops a cluster, does not terminate it. If the cluster had been saved to json, can use `get_cluster_from_json` to restart the cluster.

        Args:
            quokka_cluster (EC2Cluster): Cluster to stop.

        Return:
            None

        Examples:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager()
            >>> cluster = manager.create_cluster(aws_access_key, aws_access_id, num_instances = 2, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = ["numpy", "pandas"])
            >>> cluster.to_json("my_cluster.json")
            >>> manager.stop_cluster(cluster)
        
        """

        ec2 = boto3.client("ec2")
        instance_ids = list(quokka_cluster.instance_ids.values())
        ec2.stop_instances(InstanceIds = instance_ids)
        while True:
            time.sleep(0.1)
            a = ec2.describe_instances(InstanceIds = instance_ids)
            states = [a['Reservations'][0]['Instances'][i]['State']['Name'] for i in range(len(instance_ids))]
            if "running" in states:
                continue
            else:
                break
        quokka_cluster.state = "stopped"
        
        
    def terminate_cluster(self, quokka_cluster):

        """
        Terminate a cluster.

        Args:
            quokka_cluster (EC2Cluster): Cluster to terminate.

        Return:
            None

        Examples:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager()
            >>> cluster = manager.create_cluster(aws_access_key, aws_access_id, num_instances = 2, instance_type = "i3.2xlarge", ami="ami-0530ca8899fac469f", requirements = ["numpy", "pandas"])
            >>> manager.terminate_cluster(cluster)

        """

        ec2 = boto3.client("ec2")
        instance_ids = list(quokka_cluster.instance_ids.values())
        ec2.terminate_instances(InstanceIds = instance_ids)
        while True:
            time.sleep(0.1)
            a = ec2.describe_instances(InstanceIds = instance_ids)
            states = [a['Reservations'][0]['Instances'][i]['State']['Name'] for i in range(len(instance_ids))]
            if "running" in states:
                continue
            else:
                break
        del quokka_cluster

    
    def get_cluster_from_json(self, json_file):

        """
        Get an EC2Cluster object from a json file. The json file must have been created by `EC2Cluster.to_json`.
        This will restart the cluster if all the instances have been stopped and set up the Quokka runtime. 
        If the cluster is running, the Quokka runtime will not be set up again. So this will break if you manually turned on the instances.

        Args:
            json_file (str): Path to json file, must have been created by `EC2Cluster.to_json`. You can also manually create this json based on the 
                format of the json file created by `EC2Cluster.to_json`, but this is not recommended.

        Return:
            EC2Cluster: Cluster object.

        Examples:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager()
            >>> cluster = manager.get_cluster_from_json("my_cluster.json")
            >>> from pyquokka.df import QuokkaContext 
            >>> qc = QuokkaContext(cluster)
            >>> df = qc.read_csv("s3://my_bucket/my_file.csv")
        
        """
        
        ec2 = boto3.client("ec2")
        
        stuff = json.load(open(json_file,"r"))
        cpu_count = int(stuff["cpu_count_per_instance"])
        spill_dir = stuff["spill_dir"]
        instance_ids = self.str_key_to_int(stuff["instance_ids"])
        instance_ids = [instance_ids[i] for i in range(len(instance_ids))]
        a = ec2.describe_instances(InstanceIds = instance_ids)
        
        states = [k['State']['Name'] for reservation in a['Reservations'] for k in reservation['Instances']] 

        if sum([i=="stopped" for i in states]) == len(states):
            ec2.start_instances(InstanceIds = instance_ids)
            self._initialize_instances(instance_ids, spill_dir)
            a = ec2.describe_instances(InstanceIds = instance_ids)

            public_ips = [k['PublicIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 
            private_ips = [k['PrivateIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 

            return EC2Cluster(public_ips, private_ips, instance_ids, cpu_count, spill_dir)
        if sum([i=="running" for i in states]) == len(states):
            public_ips = [k['PublicIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 
            private_ips = [k['PrivateIpAddress'] for reservation in a['Reservations'] for k in reservation['Instances']] 
            return EC2Cluster(public_ips, private_ips, instance_ids, cpu_count, spill_dir)
        else:
            print("Cluster in an inconsistent state. Either only some machines are running or some machines have been terminated.")
            return False
    
    def get_cluster_from_ray(self, path_to_yaml, aws_access_key, aws_access_id, requirements = [], spill_dir = "/data", cluster_name = None):

        """
        Connect to a Ray cluster. This will set up the Quokka runtime on the cluster. The Ray cluster must be in a running state and created by 
        the `ray up` command. The `ray up` command creates a yaml file that is used to connect to the cluster. This function will read the yaml file
        and connect to the cluster.

        Make sure all the instances are running before calling this function! Best wait for a few minutes after calling `ray up` before calling this function.

        Args:
            path_to_yaml (str): Path to the yaml file used by `ray up`.
            aws_access_key (str): AWS access key.
            aws_access_id (str): AWS access id.
            requirements (list): List of python packages to install on the cluster.
            spill_dir (str): Directory to use for spill files. This is the directory where the Quokka runtime will write spill files.
                Quokka will detect if your instance have NVME SSD and mount it to this directory.
        
        Return:
            EC2Cluster: Cluster object.

        Examples:

            You have `us_west_2.yaml`. You call `ray up us-west-2.yaml`. You can then connect to the cluster **after all the instances are running** by doing:

            >>> from pyquokka.utils import *
            >>> manager = QuokkaClusterManager()
            >>> cluster = manager.get_cluster_from_ray("my_cluster.yaml", aws_access_key, aws_access_id, requirements = ["numpy", "pandas"], spill_dir = "/data")
            >>> from pyquokka.df import QuokkaContext
            >>> qc = QuokkaContext(cluster)

            It is recommended to do this only once and save the cluster object to a json file using `EC2Cluster.to_json` and then use `QuokkaClusterManager.get_cluster_from_json` to connect to the cluster.

            >>> cluster.to_json("my_cluster.json")
            >>> cluster = manager.get_cluster_from_json("my_cluster.json")
        
        """

        import yaml
        ec2 = boto3.client("ec2")
        with open(path_to_yaml, 'r') as f:
            config = yaml.safe_load(f)
        
    
        tag_key = "ray-cluster-name"
        if cluster_name is None:
            cluster_name = config['cluster_name']
        instance_type = config["available_node_types"]['ray.worker.default']["node_config"]["InstanceType"]
        cpu_count = ec2.describe_instance_types(InstanceTypes=[instance_type])['InstanceTypes'][0]['VCpuInfo']['DefaultVCpus']

        filters = [{'Name': 'instance-state-name', 'Values': ['running']},
                   {'Name': f'tag:{tag_key}', 'Values': [cluster_name]}]
        response = ec2.describe_instances(Filters=filters)
        instance_ids = []
        public_ips = []
        private_ips = []

        instance_names = [[k for k in instance['Tags'] if k['Key'] == 'ray-user-node-type'][0]['Value'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        instance_ids = [instance['InstanceId'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        public_ips = [instance['PublicIpAddress'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        private_ips = [instance['PrivateIpAddress'] for reservation in response['Reservations'] for instance in reservation['Instances']]

        try:
            head_index = instance_names.index("ray.head.default")
        except:
            print("No head node found. Please make sure that the cluster is running.")
            return False
    
        # rotate instance_ids, public_ips, private_ips so that head is first
        instance_ids = instance_ids[head_index:] + instance_ids[:head_index]
        public_ips = public_ips[head_index:] + public_ips[:head_index]
        private_ips = private_ips[head_index:] + private_ips[:head_index]

        assert len(instance_ids) == len(public_ips) == len(private_ips)
        print("Detected {} instances in running ray cluster {}".format(len(instance_ids), cluster_name))

        print(public_ips)
        self.set_up_envs(public_ips, requirements, aws_access_key, aws_access_id)
        self.launch_all("sudo mkdir {}".format(spill_dir), public_ips, "failed to make temp spill directory", ignore_error = True)
        self.set_up_spill_dir(public_ips, spill_dir)

        z = os.system("ssh -oStrictHostKeyChecking=no -i " + self.key_location + " ubuntu@" + public_ips[0] + " 'bash -s' < " + pyquokka.__file__.replace("__init__.py","leader_startup.sh"))
        print(z)

        self.copy_and_launch_flight(public_ips)
        return EC2Cluster(public_ips, private_ips, instance_ids, cpu_count, spill_dir)


    def get_cluster_from_dockerized_ray(self, path_to_yaml, spill_dir = "/data", cluster_name = None):

        """
        """

        import yaml
        ec2 = boto3.client("ec2")
        with open(path_to_yaml, 'r') as f:
            config = yaml.safe_load(f)
    
        tag_key = "ray-cluster-name"
        if cluster_name is None:
            cluster_name = config['cluster_name']
        instance_type = config["available_node_types"]['ray.worker.default']["node_config"]["InstanceType"]
        cpu_count = ec2.describe_instance_types(InstanceTypes=[instance_type])['InstanceTypes'][0]['VCpuInfo']['DefaultVCpus']

        filters = [{'Name': 'instance-state-name', 'Values': ['running']},
                   {'Name': f'tag:{tag_key}', 'Values': [cluster_name]}]
        response = ec2.describe_instances(Filters=filters)

        instance_names = [[k for k in instance['Tags'] if k['Key'] == 'ray-user-node-type'][0]['Value'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        instance_ids = [instance['InstanceId'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        public_ips = [instance['PublicIpAddress'] for reservation in response['Reservations'] for instance in reservation['Instances']]
        private_ips = [instance['PrivateIpAddress'] for reservation in response['Reservations'] for instance in reservation['Instances']]

        try:
            head_index = instance_names.index("ray.head.default")
        except:
            print("No head node found. Please make sure that the cluster is running.")
            return False
    
        # rotate instance_ids, public_ips, private_ips so that head is first
        instance_ids = instance_ids[head_index:] + instance_ids[:head_index]
        public_ips = public_ips[head_index:] + public_ips[:head_index]
        private_ips = private_ips[head_index:] + private_ips[:head_index]

        assert len(instance_ids) == len(public_ips) == len(private_ips)
        print("Detected {} instances in running ray cluster {}".format(len(instance_ids), cluster_name))

        print(public_ips)
        return EC2Cluster(public_ips, private_ips, instance_ids, cpu_count, spill_dir)