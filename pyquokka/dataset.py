from ast import Expression
import pickle
import pyarrow.csv as csv
import pyarrow.parquet as pq
import pyarrow.dataset as ds
from io import BytesIO, StringIO
import boto3
import s3fs
import os
import redis
#import h5py
from collections import deque
import pyarrow as pa
import polars
import numpy as np
import gc
import time

from pyarrow.fs import S3FileSystem, LocalFileSystem
from pyarrow.dataset import FileSystemDataset, ParquetFileFormat
from pyquokka.sql_utils import filters_to_expression

class SortPhase2Dataset:

    def __init__(self, channel_files, key, record_batch_rows) -> None:
        self.channel_files = channel_files
        self.record_batch_rows = record_batch_rows
        self.key = key

    def set_num_channels(self, num_channels):
        pass

    def get_next_batch(self, mapper_id, pos = None):
        # let's not support fault tolerance for now.

        if pos is not None:
            raise Exception

        import os, psutil   
        
        sources = self.channel_files[mapper_id]
        print(sources)
        number_of_batches_in_sources = [pa.ipc.open_file(pa.memory_map(source,'rb')).num_record_batches for source in sources]
        next_batch_to_gets = [1 for i in sources]
        
        process = psutil.Process(os.getpid())
        print("mem usage", process.memory_info().rss, pa.total_allocated_bytes())

        cached_batches_in_mem = [polars.from_arrow(pa.Table.from_batches([pa.ipc.open_file(pa.memory_map(source,'rb')).get_batch(0)])) for source in sources]

        while sum([len(i) != 0 for i in cached_batches_in_mem]) > 0:
        
            print("mem usage", process.memory_info().rss,  pa.total_allocated_bytes())

            disk_portions = [batch[:self.record_batch_rows] for batch in cached_batches_in_mem]
            for j in range(len(disk_portions)):
                disk_portions[j]["asdasd"] = np.ones(len(disk_portions[j])) * j
            
            result_idx = polars.concat([portion.select([self.key, "asdasd"]) for portion in disk_portions]).sort(self.key)[:self.record_batch_rows]
            disk_contribs = [(result_idx["asdasd"] == j).sum() for j in range(len(sources))]
            result = polars.concat([disk_portions[j][:disk_contribs[j]] for j in range(len(sources))]).sort(self.key)
            result.drop_in_place("asdasd")

            for j in range(len(cached_batches_in_mem)):
                cached_batches_in_mem[j] = cached_batches_in_mem[j][disk_contribs[j]:]
                
                if len(cached_batches_in_mem[j]) < self.record_batch_rows and next_batch_to_gets[j] < number_of_batches_in_sources[j]:
                    source = pa.ipc.open_file(pa.memory_map(sources[j], 'rb'))
                    next_batch = polars.from_arrow(pa.Table.from_batches([source.get_batch(next_batch_to_gets[j])]))
                    next_batch_to_gets[j] += 1
                    cached_batches_in_mem[j].vstack(next_batch, in_place = True)
                    del next_batch
            
            print(gc.collect())
            yield None, result


class RedisObjectsDataset:

    # expects objects as a dict of channel : list of tuples of (ip, key, size)
    def __init__(self, channel_objects, ip_set) -> None:
        self.channel_objects = channel_objects
        self.rs = {}
        self.ip_set = ip_set
        for ip in self.ip_set:
            self.rs[ip] = redis.Redis(host=ip, port=6800, db=0)

    def get_next_batch(self, mapper_id, pos=None):
        if mapper_id not in self.channel_objects:
            raise Exception(
                "ERROR: I dont know about where this channel is. Autoscaling here not supported yet. Will it ever be?")

        total_objects = len(self.channel_objects[mapper_id])

        if pos is None:
            pos = 0

        while pos < total_objects:
            object = self.channel_objects[mapper_id][pos]
            bump = self.rs[object[0]].get(object[1])
            pos += 1
            yield pos, pickle.loads(bump)


class InputDiskHDF5Dataset:

    def __init__(self, filename, key) -> None:
        
        self.filename = filename
        self.key = key

        self.num_channels = None
        
    def set_num_channels(self, num_channels):
        self.num_channels = num_channels
        self.h5file = h5py.File(self.filename)
        self.dataset = self.h5file[self.key]
        self.chunk_size = self.dataset.chunks
        self.dataset_shape = self.dataset.shape

        assert self.chunk_size[1] == self.dataset_shape[1]
        self.num_chunks = (self.dataset_shape[0]-1) // self.chunk_size[0] + 1

    def get_next_batch(self, mapper_id, pos=None):
        assert self.num_channels is not None
        if pos is None:
            curr_chunk = mapper_id
        else:
            curr_chunk = pos
        while curr_chunk < self.num_chunks:
            chunk_start = curr_chunk * self.chunk_size[0]            
            result = self.dataset[chunk_start:chunk_start + self.chunk_size[0]]
            curr_chunk += self.num_channels
            yield curr_chunk, result

# the only difference here is that we move the fragment construction inside get_batches
# this is because on cluster setting we want to do that instead of initializing locally
# on local setting you want to do the reverse! 
class InputEC2ParquetDataset:
    def __init__(self, filepath, mode = "local", columns = None, filters = None) -> None:
        
        self.filepath = filepath
        self.columns = columns
        if filters is not None:
            if type(filters) == list:
                self.filters = filters_to_expression(filters)
            elif type(filters) == ds.Expression:
                self.filters = filters
            else:
                raise Exception("cannot understand filters format.")
        else:
            self.filters = None
        self.mode = mode
        self.num_channels = None
    
    def set_num_channels(self, num_channels):
        assert self.num_channels == num_channels

    def get_own_state(self, num_channels):

        print("Initializing Parquet dataset. This is currently done locally and serially, which might take a while.")

        self.num_channels = num_channels
        if self.mode == "s3":
            s3 = S3FileSystem()
            dataset = ds.dataset(self.filepath, filesystem = s3)
        else:
            dataset = ds.dataset(self.filepath)


        self.schema = dataset.schema
        total_rows = dataset.count_rows()
        print("Parquet dataset at ", self.filepath, " has total ", total_rows, " rows")
        row_group_fragments = [fragment.split_by_row_group() for fragment in dataset.get_fragments()]
        row_group_fragments_with_size = [(item.count_rows(), item) for sublist in row_group_fragments for item in sublist]
        row_group_fragments_with_size.sort(key = lambda x: x[0])

        self.channel_assigments = {i: [] for i in range(num_channels)}
        channel_lengths = np.array([0] * num_channels)

        '''
        Hey we encounter the Partition Problem! We would like to evenly divide the row groups based on length.
        We will use Greedy number partitioning. The easiest to implement approximate algorithm.
        '''
        
        for size, fragment in row_group_fragments_with_size:
            channel = np.argmin(channel_lengths)
            self.channel_assigments[channel].append((fragment.path, fragment.partition_expression))
            channel_lengths[channel] += size

    def get_next_batch(self, mapper_id, pos=None):
        assert self.num_channels is not None
        if pos is None:
            pos = 0
        format = ParquetFileFormat()
        filesystem = S3FileSystem() if self.mode == "s3" else LocalFileSystem()
        fragments = [
            format.make_fragment(
                file,
                filesystem=filesystem,
                partition_expression=part_expression,
            )
            for file, part_expression in self.channel_assigments[mapper_id]
        ]

        self.dataset = FileSystemDataset(fragments[pos:], self.schema, format , filesystem)
        for batch in self.dataset.to_batches(filter= self.filters,columns=self.columns ):
            pos += 1
            yield pos, batch

class InputParquetDataset:
    def __init__(self, filepath, mode = "local", columns = None, filters = None) -> None:
        
        self.filepath = filepath
        self.columns = columns
        if filters is not None:
            if type(filters) == list:
                self.filters = filters_to_expression(filters)
            elif type(filters) == ds.Expression:
                self.filters = filters
            else:
                raise Exception("cannot understand filters format.")
        else:
            self.filters = None
        self.mode = mode
        self.num_channels = None
    
    def set_num_channels(self, num_channels):
        assert self.num_channels == num_channels

    def get_own_state(self, num_channels):

        self.num_channels = num_channels
        if self.mode == "s3":
            s3 = S3FileSystem()
            dataset = ds.dataset(self.filepath, filesystem = s3)
        else:
            dataset = ds.dataset(self.filepath)

        self.schema = dataset.schema
        total_rows = dataset.count_rows()
        print("Parquet dataset at ", self.filepath, " has total ", total_rows, " rows")
        row_group_fragments = [fragment.split_by_row_group() for fragment in dataset.get_fragments()]
        row_group_fragments_with_size = [(item.count_rows(), item) for sublist in row_group_fragments for item in sublist]
        row_group_fragments_with_size.sort(key = lambda x: x[0])

        self.channel_assigments = {i: [] for i in range(num_channels)}
        channel_lengths = np.array([0] * num_channels)

        '''
        Hey we encounter the Partition Problem! We would like to evenly divide the row groups based on length.
        We will use Greedy number partitioning. The easiest to implement approximate algorithm.
        '''
        
        for size, fragment in row_group_fragments_with_size:
            channel = np.argmin(channel_lengths)
            self.channel_assigments[channel].append(fragment)
            channel_lengths[channel] += size

    def get_next_batch(self, mapper_id, pos=None):
        assert self.num_channels is not None
        if pos is None:
            pos = 0
        format = ParquetFileFormat()
        filesystem = S3FileSystem() if self.mode == "s3" else LocalFileSystem()

        self.dataset = FileSystemDataset(self.channel_assigments[mapper_id][pos:], self.schema, format , filesystem)
        for batch in self.dataset.to_batches(filter= self.filters,columns=self.columns ):
            pos += 1
            yield pos, batch

# this works for a directoy of objects.
class InputS3FilesDataset:

    def __init__(self, bucket, prefix= None) -> None:
        
        self.bucket = bucket
        self.prefix = prefix
        
        self.num_channels = None

    def set_num_channels(self, num_channels):
        self.num_channels = num_channels
        self.s3 = boto3.client('s3')
        if self.prefix is not None:
            z = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix)
            self.files = [i['Key'] for i in z['Contents']]
            while 'NextContinuationToken' in z.keys():
                z = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix, ContinuationToken=z['NextContinuationToken'])
                self.files.extend([i['Key'] for i in z['Contents']])
        else:
            z = self.s3.list_objects_v2(Bucket=self.bucket)
            self.files = [i['Key'] for i in z['Contents']]
            while 'NextContinuationToken' in z.keys():
                z = self.s3.list_objects_v2(Bucket=self.bucket, ContinuationToken=z['NextContinuationToken'])
                self.files.extend([i['Key'] for i in z['Contents']])

    def get_next_batch(self, mapper_id, pos=None):
        assert self.num_channels is not None
        if pos is None:
            curr_pos = mapper_id
        else:
            curr_pos = pos
        while curr_pos < len(self.files):
            #print("input batch", (curr_pos - mapper_id) / self.num_channels)
            # since these are arbitrary byte files (most likely some image format), it is probably useful to keep the filename around or you can't tell these things apart
            a = (self.files[curr_pos], self.s3.get_object(Bucket=self.bucket, Key=self.files[curr_pos])['Body'].read())
            #print("ending reading ",time.time())
            curr_pos += self.num_channels
            yield curr_pos, a


       
# this should work for 1 CSV up to multiple
class InputDiskCSVDataset:
    def __init__(self, filepath , names = None , sep=",", stride=16 * 1024 * 1024, header = False, window = 1024 * 4) -> None:
        self.filepath = filepath

        self.num_channels = None
        self.names = names
        self.sep = sep
        self.stride = stride
        self.header = header
        
        self.window = window
        assert not (names is None and header is False), "if header is False, must supply column names"

        self.length = 0
        #self.sample = None

    def set_num_channels(self, num_channels):
        assert self.num_channels == num_channels
        self.s3 = boto3.client('s3')  # needs boto3 client
    
    # we need to rethink this whole setting num channels business. For this operator we don't want each node to do redundant work!
    def get_own_state(self, num_channels):
        
        #samples = []
        print("Initializing Disk CSV dataset. This is currently done locally and serially, which might take a while.")
        self.num_channels = num_channels

        '''
        Filepath can be either a single file or a directory. We need to figure out what is the case, and get our files and sizes list.
        '''

        if os.path.isfile(self.filepath):
            files = deque([self.filepath])
            sizes = deque([os.path.getsize(self.filepath)])
        else:
            assert os.path.isdir(self.filepath), "Does not support prefix, must give absolute directory path for a list of files, will read everything in there!"
            files = deque([os.listdir(self.filepath)])
            sizes = deque([os.path.getsize(self.filepath + "/" + file) for file in files])

        if self.header:
            resp = open(files[0],"r").read(self.window)
            first_newline = resp.find("\n")
            if first_newline == -1:
                raise Exception("could not detect the first line break. try setting the window argument to a large number")
            if self.names is None:
                self.names = resp[:first_newline].split(",")
            else:
                detected_names = resp[:first_newline].split(",")
                if self.names != detected_names:
                    print("Warning, detected column names from header row not the same as supplied column names!")
                    print("Detected", detected_names)
                    print("Supplied", self.names)

        total_size = sum(sizes)
        size_per_channel = total_size // num_channels
        channel_infos = {}

        start_byte = 0
        real_off = 0

        for channel in range(num_channels):
            my_file = []
            curr_size = 0
            done = False
            while curr_size + sizes[0] <= size_per_channel:
                my_file.append(files.popleft())
                end_byte = sizes.popleft()
                if len(sizes) == 0:
                    done = True
                    break # we are taking off a bit more work each time so the last channel might leave before filling up size per channel
                curr_size += end_byte
                real_off = 0
        
            if done:
                channel_infos[channel] = (start_byte, my_file.copy(), real_off + end_byte)
                break

            #curr_size + size[0] > size_per_channel, the leftmost file has enough bytes to satisfy the channel
            # this never gonna happen in real life
            if curr_size == size_per_channel:
                channel_infos[channel] = (start_byte, my_file.copy(), real_off + end_byte)
                continue

            my_file.append(files[0])
            candidate = size_per_channel - curr_size

            start = max(0, candidate - self.window)
            end = min(candidate + self.window, sizes[0])

            f = open(files[0],"rb")
            f.seek(real_off + start)
            resp = f.read(end - start)
            
            first_newline = resp.find(bytes('\n', 'utf-8'))
            last_newline = resp.rfind(bytes('\n', 'utf-8'))
            if last_newline == -1:
                raise Exception
            else:
                bytes_to_take = start + last_newline + 1
                #samples.append(csv.read_csv(BytesIO(resp[first_newline: last_newline]), read_options=csv.ReadOptions(
                #    column_names=self.names), parse_options=csv.ParseOptions(delimiter=self.sep)))

            sizes[0] -= bytes_to_take
            end_byte = real_off + bytes_to_take
            real_off += bytes_to_take
            channel_infos[channel] = (start_byte, my_file.copy(), end_byte)
            start_byte = real_off
        
        self.length = total_size
        #self.sample = pa.concat_tables(samples)
        self.channel_infos = channel_infos
        print(self.channel_infos)
        print("initialized CSV reading strategy for ", total_size // 1024 // 1024 // 1024, " GB of CSV")


    def get_next_batch(self, mapper_id, state = None):
        assert self.num_channels is not None
        files = self.channel_infos[mapper_id][1]
        
        if state is None:
            curr_pos = 0
            pos = self.channel_infos[mapper_id][0]
        else:
            curr_pos, pos = state
            
        while curr_pos < len(files):
            
            file = files[curr_pos]
            if curr_pos != len(files) - 1:
                end = os.path.getsize(file)
            else:
                end = self.channel_infos[mapper_id][2]

            f = open(file, "rb")   
            f.seek(pos)         
            while pos < end:
                f.seek(pos)

                bytes_to_read = min(pos+self.stride, end) - pos
                resp = f.read(bytes_to_read)
                
                #print(pos, bytes_to_read)
                
                last_newline = resp.rfind(bytes('\n', 'utf-8'))

                if last_newline == -1:
                    raise Exception
                else:
                    resp = resp[:last_newline]

                    if self.header and pos == 0:
                        first_newline = resp.find(bytes('\n','utf-8'))
                        if first_newline == -1:
                            raise Exception
                        resp = resp[first_newline + 1:]

                    bump = csv.read_csv(BytesIO(resp), read_options=csv.ReadOptions(
                        column_names=self.names), parse_options=csv.ParseOptions(delimiter=self.sep))
                    
                    pos += last_newline + 1
                    
                    yield (curr_pos, pos) , polars.from_arrow(bump)

            pos = 0
            curr_pos += 1


# this should work for 1 CSV up to multiple
class InputS3CSVDataset:
    def __init__(self, bucket, names = None, prefix = None, key = None, sep=",", stride=64 * 1024 * 1024, header = False, window = 1024 * 32) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.key = key

        assert (self.prefix is None and self.key is not None) or (self.prefix is not None and self.key is None)

        self.num_channels = None
        self.names = names
        self.sep = sep
        self.stride = stride
        self.header = header
        
        self.window = window
        assert not (names is None and header is False), "if header is False, must supply column names"

        self.length = 0
        #self.sample = None

    def set_num_channels(self, num_channels):
        assert self.num_channels == num_channels
        self.s3 = boto3.client('s3')  # needs boto3 client
    
    # we need to rethink this whole setting num channels business. For this operator we don't want each node to do redundant work!
    def get_own_state(self, num_channels):
        
        #samples = []
        print("Initializing S3 CSV dataset. This is currently done locally and serially, which might take a while.")
        self.num_channels = num_channels

        s3 = boto3.client('s3')  # needs boto3 client, however it is transient and is not part of own state, so Ray can send this thing! 
        if self.key is not None:
            files = deque([self.key])
            response = s3.head_object(Bucket=self.bucket, Key=self.key)
            sizes = deque([response['ContentLength']])
        else:
            if self.prefix is not None:
                z = s3.list_objects_v2(Bucket=self.bucket, Prefix=self.prefix)
                files = z['Contents']
                while 'NextContinuationToken' in z.keys():
                    z = s3.list_objects_v2(
                        Bucket=self.bucket, Prefix=self.prefix, ContinuationToken=z['NextContinuationToken'])
                    files.extend(z['Contents'])
            else:
                z = s3.list_objects_v2(Bucket=self.bucket)
                files = z['Contents']
                while 'NextContinuationToken' in z.keys():
                    z = s3.list_objects_v2(
                        Bucket=self.bucket, ContinuationToken=z['NextContinuationToken'])
                    files.extend(z['Contents'])
            sizes = deque([i['Size'] for i in files])
            files = deque([i['Key'] for i in files])

        if self.header:
            resp = s3.get_object(
                Bucket=self.bucket, Key=files[0], Range='bytes={}-{}'.format(0, self.window))['Body'].read()
            first_newline = resp.find(bytes('\n', 'utf-8'))
            if first_newline == -1:
                raise Exception("could not detect the first line break. try setting the window argument to a large number")
            if self.names is None:
                self.names = resp[:first_newline].decode("utf-8").split(",")
            else:
                detected_names = resp[:first_newline].decode("utf-8").split(",")
                if self.names != detected_names:
                    print("Warning, detected column names from header row not the same as supplied column names!")
                    print("Detected", detected_names)
                    print("Supplied", self.names)

        total_size = sum(sizes)
        size_per_channel = total_size // num_channels
        channel_infos = {}

        start_byte = 0
        real_off = 0

        for channel in range(num_channels):
            my_file = []
            curr_size = 0
            done = False
            while curr_size + sizes[0] <= size_per_channel:
                my_file.append(files.popleft())
                end_byte = sizes.popleft()
                if len(sizes) == 0:
                    done = True
                    break # we are taking off a bit more work each time so the last channel might leave before filling up size per channel
                curr_size += end_byte
                real_off = 0
        
            if done:
                channel_infos[channel] = (start_byte, my_file.copy(), real_off + end_byte)
                break

            #curr_size + size[0] > size_per_channel, the leftmost file has enough bytes to satisfy the channel
            # this never gonna happen in real life
            if curr_size == size_per_channel:
                channel_infos[channel] = (start_byte, my_file.copy(), real_off + end_byte)
                continue

            my_file.append(files[0])
            candidate = size_per_channel - curr_size

            start = max(0, candidate - self.window)
            end = min(candidate + self.window, sizes[0])

            resp = s3.get_object(
                Bucket=self.bucket, Key=files[0], Range='bytes={}-{}'.format(real_off + start,real_off + end))['Body'].read()
            first_newline = resp.find(bytes('\n', 'utf-8'))
            last_newline = resp.rfind(bytes('\n', 'utf-8'))
            if last_newline == -1:
                raise Exception
            else:
                bytes_to_take = start + last_newline
                #samples.append(csv.read_csv(BytesIO(resp[first_newline: last_newline]), read_options=csv.ReadOptions(
                #    column_names=self.names), parse_options=csv.ParseOptions(delimiter=self.sep)))

            sizes[0] -= bytes_to_take
            end_byte = real_off + bytes_to_take
            real_off += bytes_to_take
            channel_infos[channel] = (start_byte, my_file.copy(), end_byte)
            start_byte = real_off
        
        self.length = total_size
        #self.sample = pa.concat_tables(samples)
        self.channel_infos = channel_infos
        print("initialized CSV reading strategy for ", total_size // 1024 // 1024 // 1024, " GB of CSV")


    def get_next_batch(self, mapper_id, state = None):
        assert self.num_channels is not None
        files = self.channel_infos[mapper_id][1]
        
        if state is None:
            curr_pos = 0
            pos = self.channel_infos[mapper_id][0]
        else:
            curr_pos, pos = state
            
        while curr_pos < len(files):
            
            file = files[curr_pos]
            if curr_pos != len(files) - 1:
                response = self.s3.head_object(
                    Bucket=self.bucket,
                    Key= file
                )
                length = response['ContentLength']
                end = length
            else:
                end = self.channel_infos[mapper_id][2]

            
            while pos < end-1:

                resp = self.s3.get_object(Bucket=self.bucket, Key=file, Range='bytes={}-{}'.format(
                    pos, min(pos+self.stride, end)))['Body'].read()
                last_newline = resp.rfind(bytes('\n', 'utf-8'))

                if last_newline == -1:
                    raise Exception
                else:
                    resp = resp[:last_newline]

                    if self.header and pos == 0:
                        first_newline = resp.find(bytes('\n','utf-8'))
                        if first_newline == -1:
                            raise Exception
                        resp = resp[first_newline + 1:]

                    bump = csv.read_csv(BytesIO(resp), read_options=csv.ReadOptions(
                        column_names=self.names), parse_options=csv.ParseOptions(delimiter=self.sep))
                    
                    pos += last_newline
                    
                    yield (curr_pos, pos) , polars.from_arrow(bump)

            pos = 0
            curr_pos += 1



