import nidaqmx as dq
import matplotlib.pyplot as plt
import numpy as np
import math
from collections import deque
import random


"""
This file contains classes that are responsbile for communicating with DAQ devices, writing and reading the data.
The class DAQ_tasks is the one that is usually used by GUI classes or by the TransferLock class. It containes in
itself references to objects of three other classes defined here: Scan class, controlling cavity scan, L_task class,
which controls voltages applied to science lasers (so controls their frequencies), and PD_task class, which is 
designed to read data from photodetectors for both the master and slave lasers.The whole process of scanning (so 
writing data) and reading is managed from the level of DAQ_tasks. It also contains some more general helpful methods.
"""
class DAQ_tasks:

	"""
	The class can be initialized with device name, if read from a config file. Then, it searches through all DAQs
	that are connected to this computer (might include a smiulated DAQ) and chooses one that matches the name.
	Otherwise, it chooses the first one from the list.
	"""
	def __init__(self,simulate,dev_name=None):
		
		syst=dq.system.System.local()
		if dev_name is not None:
			for dev in syst.devices:
				if dev.name==dev_name:
					self.device=dev
					break
			else:
				raise NameError('Could not locate DAQ device of given name.')
		else:
			self.device=syst.devices[0]
		self.ao_scan=0
		self.ao_laser=0
		self.ai_PDs=0
		self.power_PDs=0
		self.time_samples=[]
		self.PD_data=[]
		self.simulation=simulate


	#To avoid error when the program is being closed, the tasks are closed first.
	def __del__(self):
		self._clear_tasks()


	#Function that clears the task and removes references them from their classes.
	def _clear_tasks(self):
		self.ao_scan.dq_task.close()
		self.ao_scan.dq_task=0
		self.ao_laser.dq_task.close()
		self.ao_laser.dq_task=0
		self.ai_PDs.dq_task.close()
		self.ai_PDs.dq_task=0
		self.power_PDs.dq_task.close()
		self.power_PDs.dq_task=0

	
	"""
	If user changes channels for a task, and then wants to go back to default configuration, this function is invoked.
	It clears tasks and creates fresh ones using the config file.
	"""
	def reset_tasks(self,cfg,n):
		self._clear_tasks()

		self.ao_scan.dq_task=dq.Task(new_task_name="Scan")
		self.ao_scan.dq_task.ao_channels.add_ao_voltage_chan(self.device.name+"/ao"+cfg['CAVITY']['OutputChannel'])

		self.ao_laser.dq_task=dq.Task(new_task_name="Lasers")
		self.ao_laser._channel_no=0

		self.power_PDs.dq_task=dq.Task(new_task_name="Power")
		self.power_PDs._channel_no=0

		self.ai_PDs.dq_task=dq.Task(new_task_name="PDs")
		self.ai_PDs.dq_task.ai_channels.add_ai_voltage_chan(self.device.name+"/ai"+cfg['CAVITY']['InputChannel'])
		self.ai_PDs._channel_no=1

		self.add_laser(int(cfg['LASER1']['InputChannel']),int(cfg['LASER1']['OutputChannel']),int(cfg['LASER1']['PowerChannel']))
		if n>1:
			self.add_laser(int(cfg['LASER2']['InputChannel']),int(cfg['LASER2']['OutputChannel']),int(cfg['LASER2']['PowerChannel']))

		#Timing (synchronisation) has to be set every time we recreate a task.
		self.set_input_timing()
	

	#Function similar to the previous one. This one is invoked when user changes at least one channel.
	def update_tasks(self,ao_channels,ai_channels,power_channels):
		self._clear_tasks()

		self.ao_scan.dq_task=dq.Task(new_task_name="Scan")
		self.ao_scan.dq_task.ao_channels.add_ao_voltage_chan(ao_channels[0])

		self.ao_laser.dq_task=dq.Task(new_task_name="Lasers")
		for ch in ao_channels[1:]:
			self.ao_laser.dq_task.ao_channels.add_ao_voltage_chan(ch)

		self.ai_PDs.dq_task=dq.Task(new_task_name="PDs")
		for ch in ai_channels:
			self.ai_PDs.dq_task.ai_channels.add_ai_voltage_chan(ch)

		self.power_PDs.dq_task=dq.Task(new_task_name="Power")
		for ch in power_channels:
			self.power_PDs.dq_task.ai_channels.add_ai_voltage_chan(ch)

		try:	
			self.set_input_timing()
		except:
			raise Exception('Input timing was not set.')


	#A couple of self-explanatory methods.
	def get_ao_channel_names(self):
		return self.device.ao_physical_chans.channel_names

	def get_ai_channel_names(self):
		return self.device.ai_physical_chans.channel_names

	def get_scan_ao_channel(self):
		return self.ao_scan.dq_task.channel_names[0]

	def get_scan_ai_channel(self):
		return self.ai_PDs.dq_task.channel_names[0]

	def get_laser_ao_channel(self,ind):
		return self.ao_laser.dq_task.channel_names[ind]

	def get_laser_ai_channel(self,ind):
		return self.ai_PDs.dq_task.channel_names[ind+1]

	def get_laser_power_channel(self,ind):
		return self.power_PDs.dq_task.channel_names[ind]

	def get_all_used_ai_channels(self):
		return self.ai_PDs.dq_task.channel_names

	def get_all_used_ao_channels(self):
		return self.ao_scan.dq_task.channel_names+self.ao_laser.dq_task.channel_names


	#Creating an object of Scan class and adding reference to an attribute of this class.
	def set_scan_task(self,name,channel=0):
		self.ao_scan=Scan(self.device,name,channel)


	"""
	Method that configures scannign at the very beginning of creating object of Scan class. These changes are
	made to the class, not the task, so when tasks are cleared or reset, these parameters stay untouched. Note,
	that "set_input_timing" method should be called after this function, though here intentionally it is left out
	(it should be called once all the tasks are set up, and this method is only called at the beginning of setting
	up just the scan task).
	"""
	def setup_scanning(self,mn_voltage,mx_voltage,offset,amp,n_samp,scan_t):
		
		#Maximum and minimum voltage for a scan is set
		self.ao_scan.configure_voltage_boundaries(mn_voltage,mx_voltage)

		#Scanning offset, scan amplitude and number of samples are set.
		self.ao_scan.configure_scan_voltages(offset,amp,n_samp)

		#Then, scanning rate and scanning time can be set.
		self.ao_scan.configure_scan_sampling(scan_t)

		#Finally, because we are plotting acquired data as a function of time, we create the X-axis for the plot.
		self.time_samples=np.linspace(0,scan_t,num=n_samp)


	"""
	Similar function that modifies only scanning parameters accessible from the main part of GUI. It updates
	clock settings and synchronisation at the end (it should be called once all other tasks are set up).
	"""
	def modify_scanning(self,offset,amp,n_samp,scan_t):
		self.ao_scan.configure_scan_voltages(offset,amp,n_samp)
		self.ao_scan.configure_scan_sampling(scan_t)
		self.time_samples=np.linspace(0,scan_t,num=n_samp)
		self.set_input_timing()


	#Creates an instance of L_task class
	def set_laser_task(self,name):
		self.ao_laser=L_task(self.device,name)


	#Method setting voltages of the lasers (so it sets their frequencies)
	def set_laser_volts(self,voltages):
		self.ao_laser.configure_voltages(voltages)


	#Adjusting maximum and minimum voltages allowed for both slave lasers.
	def set_laser_voltage_boundaries(self,mn_voltages,mx_voltages):
		self.ao_laser.configure_voltage_boundaries(mn_voltages,mx_voltages)


	#Creating an object of PD_task class. It automatically sets up a task for master laser photodetection.
	def set_PD_task(self,name,scan_channel=0):
		self.ai_PDs=PD_task(self.device,name,scan_channel)


	#Creating an object of power_PD_task class.
	def set_power_task(self,name):
		self.power_PDs=Power_PD_task(self.device,name)


	#Method adding a laser. It adds channels to L_task tasks and to PD_task tasks. 
	def add_laser(self,in_channel,out_channel,power_channel):
		self.ao_laser.add_laser(out_channel)
		self.ai_PDs.add_laser(in_channel)
		self.power_PDs.add_laser(power_channel)


	"""
	Method synchronising readout clock with writing clock - samples have to be read from photodetectors at the 
	same points when scan is performed.
	"""
	def set_input_timing(self):
		self.ai_PDs.configure_clock(self.ao_scan.sample_rate,self.ao_scan.n_samples)


	#Method that manages scanning and acquiring data from the DAQ.
	def scan_and_acquire(self,evnt):

		#The task for collecting data is started, but the data is not collected yet.
		self.ai_PDs.start()

		#Voltages for the lasers are set and the scan is performed. Both tasks start and are performed automatically.
		self.ao_laser.set_voltages(True)
		self.ao_scan.perform_scan(True)

		#Data from photodetectros is acquired (it was stored in buffers when scan was being performed, now it's fetched)
		self.ai_PDs.acquire_data()

		#We set options for the program to wait for scan and readout to bo completed before the task is stopped.
		self.ao_scan.dq_task.wait_until_done()
		self.ai_PDs.dq_task.wait_until_done()

		
		#We add reference to the DAQ_task object
		self.PD_data=self.ai_PDs.acq_data

		#We stop the tasks. 
		self.ao_scan.dq_task.stop()
		self.ai_PDs.dq_task.stop()

		self.get_power()

		if self.simulation:
			self.PD_data=self.simulate_scan()

		#Flag is set
		evnt.set()


	def get_power(self):

		self.power_PDs.start()
		self.power_PDs.acquire_data(self.simulation)
		self.power_PDs.stop()


	def simulate_scan(self):
		peak_m1=(self.ao_scan.mx_voltage/10-self.ao_scan.offset)+self.ao_scan.scan_time/8
		peak_m2=peak_m1+self.ao_scan.scan_time*0.5

		peak_s1=self.ao_laser.voltages[0]/5*self.ao_scan.scan_time
		peak_s12p=peak_s1+(peak_m2-peak_m1)*1000/784.5
		peak_s12m=peak_s1-(peak_m2-peak_m1)*1000/784.5
		
		M=generate_data([0.01,0.01],[peak_m1,peak_m2],[2/self.ao_scan.n_samples*self.ao_scan.scan_time,2/self.ao_scan.n_samples*self.ao_scan.scan_time],self.ao_scan.n_samples,0,self.ao_scan.scan_time)
		M=add_noise(M,0.002)
		
		S1=generate_data([0.002,0.002,0.002],[peak_s1,peak_s12p,peak_s12m],[1/self.ao_scan.n_samples*self.ao_scan.scan_time,1/self.ao_scan.n_samples*self.ao_scan.scan_time,1/self.ao_scan.n_samples*self.ao_scan.scan_time],self.ao_scan.n_samples,0,self.ao_scan.scan_time)
		S1=add_noise(S1,0.001)

		if self.ao_laser._channel_no>1:
			peak_s2=self.ao_laser.voltages[1]/5*self.ao_scan.scan_time
			peak_s22p=peak_s2+(peak_m2-peak_m1)*1000/784.5
			peak_s22m=peak_s2-(peak_m2-peak_m1)*1000/784.5
			S2=generate_data([0.002,0.002,0.002],[peak_s2,peak_s22p,peak_s22m],[1/self.ao_scan.n_samples*self.ao_scan.scan_time,1/self.ao_scan.n_samples*self.ao_scan.scan_time,1/self.ao_scan.n_samples*self.ao_scan.scan_time],self.ao_scan.n_samples,0,self.ao_scan.scan_time)
			S2=add_noise(S2,0.0015)
		
			return [M,S1,S2]
		else:
			return [M,S1]

	

#################################################################################################################


"""
The class below handles the scanning procedure. It writes data to the DAQ with sampling rate defined by user (Through 
number of samples per scan and scanning time).

"""
class Scan:

	#We initialize by creating a DAQ Task and add an analog output channel used for the scan (channel number is in config file)
	def __init__(self,dev,name,channel):
		self.dq_task=dq.Task(new_task_name=name)
		self.dq_task.ao_channels.add_ao_voltage_chan(dev.name+"/ao"+str(channel))
		self.n_samples=0
		self.scan_time=0
		self.sample_rate=0
		self.scan_points=0
		self.scan_step=0
		self.offset=0
		self.mn_voltage=0
		self.mx_voltage=0
		self.scan_end=0
		self.amplitude=0


	#Starting the task. Used if autostart is not used.
	def start(self):
		self.dq_task.start()
	

	#Setting maximum and minimum voltage for cavity.
	def configure_voltage_boundaries(self,mn_voltage,mx_voltage):
		if mn_voltage>=mx_voltage:
			mx_voltage,mn_voltage=mn_voltage,mx_voltage
		self.mn_voltage=mn_voltage
		self.mx_voltage=mx_voltage


	#Configuring scan offset, amplitued and number of samples.
	def configure_scan_voltages(self,offset,amplitude,n_samples):

		self.n_samples=int(n_samples)

		if offset<self.mn_voltage:
			offset=self.mn_voltage
		if offset>self.mx_voltage:
			offset=self.mx_voltage

		if amplitude+offset>self.mx_voltage:
			self.amplitude=self.mx_voltage-offset
		else:
			self.amplitude=amplitude
		
		self.offset=offset

		self.scan_end=offset+self.amplitude

		#These are the points that will be writting to the DAQ (and then to cavity's piezo)
		self.scan_points=np.linspace(self.offset,self.scan_end,num=self.n_samples)

		self.scan_step=self.scan_points[1]-self.scan_points[0]


	#Method configuring scan sampling rate using number of samples per scan and the scan time.
	def configure_scan_sampling(self,scan_time):

		self.scan_time=scan_time #ms
		self.sample_rate=1000*self.n_samples/scan_time #S/s

		#The clock is configured using sample rate.
		self.dq_task.timing.cfg_samp_clk_timing(self.sample_rate,samps_per_chan=self.n_samples)

		#We also need to adjust size of the buffer and set it to the number of samples that are supposed to be written. 
		self.dq_task.out_stream.output_buf_size=self.n_samples


	#Method performing writing data to DAQ.
	def perform_scan(self,autostart_flag):

		self.dq_task.write(self.scan_points,auto_start=autostart_flag)


	#Setting scanning offset. It has to modify all the scanning points. 
	def set_offset(self,offset):

		if offset<self.mn_voltage:
			offset=self.mn_voltage
		if offset+self.amplitude>self.mx_voltage:
			offset=self.mx_voltage-self.amplitude

		self.offset=offset

		self.scan_end=offset+self.amplitude

		self.scan_points=np.linspace(self.offset,self.scan_end,num=self.n_samples)


	#Moving scanning offset. It has to move all the scanning points. 
	def move_offset(self,change):
		self.offset+=change

		if self.offset<self.mn_voltage:
			self.offset=self.mn_voltage
		if self.offset+self.amplitude>self.mx_voltage:
			self.offset=self.mx_voltage-self.amplitude

		self.scan_end=self.offset+self.amplitude

		self.scan_points=np.linspace(self.offset,self.scan_end,num=self.n_samples)


#################################################################################################################


"""
This class handles simple task of adjusting voltage applied to slave lasers. Initialization just creates the DAQ Task,
but doesn't add any channels.
"""
class L_task:

	def __init__(self,dev,name):
		self.dq_task=dq.Task(new_task_name=name)
		self.device=dev
		self.voltages=[]
		self.mn_voltages=[]
		self.mx_voltages=[]

		#Number of slave lasers/channels used
		self._channel_no=0
	

	#Configuration of maximum and minimum voltages for all lasers.
	def configure_voltage_boundaries(self,mn_voltages,mx_voltages):
		for i in range(self._channel_no):
			if mn_voltages[i]>=mx_voltages[i]:
				mx_voltages[i],mn_voltages[i]=mn_voltages[i],mx_voltages[i]
		self.mn_voltages=mn_voltages
		self.mx_voltages=mx_voltages


	#Maximum and minimum voltage for only one laser
	def configure_voltage_boundary(self,mn_voltage,mx_voltage,ind):
		if mn_voltage>=mx_voltage:
			mx_voltage,mn_voltage=mn_voltage,mx_voltage
		self.mn_voltages[ind]=mn_voltage
		self.mx_voltages[ind]=mx_voltage

	
	#Adding a laser. Method just adds analog output channel associated with a slave laser.
	def add_laser(self,channel):

		self.dq_task.ao_channels.add_ao_voltage_chan(self.device.name+"/ao"+str(channel))
		self._channel_no+=1


	#Configuring voltages that are to be set for the lasers.
	def configure_voltages(self,voltages):
		if len(voltages)!=self._channel_no:
			raise ValueError('Wrong number of voltages')
		for i in range(self._channel_no):
			if voltages[i]<self.mn_voltages[i]:
				voltages[i]=self.mn_voltages[i]
			if voltages[i]>self.mx_voltages[i]:
				voltages[i]=self.mx_voltages[i]
		self.voltages=voltages
		

	#Method actually setting those voltages through the DAQ.
	def set_voltages(self,as_flag):
		self.dq_task.write(self.voltages,auto_start=as_flag)


#################################################################################################################


"""
Class that takes care of reading the data from photodetectors through the DAQ. It initializes by creating a DAQ Task
and by adding the first channel for the master (cavity reference) laser.
"""
class PD_task:

	def __init__(self,dev,name,scan_channel):
		self.dq_task=dq.Task(new_task_name=name)
		self.device=dev
		self.dq_task.ai_channels.add_ai_voltage_chan(dev.name+"/ai"+str(scan_channel))
		self.acq_data=[]
		self.n_samples=0

		#Eventually equal to master laser + number of slave lasers.
		self._channel_no=1


	#Starting the task. Reading data is usually not started automatically.
	def start(self):
		self.dq_task.start()


	#Adds an analog input channel connected to the photodetector that is associated with one of the slave lasers.
	def add_laser(self,channel):
		self.dq_task.ai_channels.add_ai_voltage_chan(self.device.name+"/ai"+str(channel))
		self._channel_no+=1

	"""
	Synchronisation of the clock for this (read) task with the clock used to write voltages to the cavity (write task).
	For that we're basically saying that clock for this task is to be the same as for the write task. It also automatically
	adopts the buffer size from the write task.
	"""
	def configure_clock(self,sample_rate,n_samples):
		try:
			self.dq_task.timing.cfg_samp_clk_timing(sample_rate,source='/'+self.device.name+'/ao/SampleClock',samps_per_chan=n_samples)
			self.n_samples=n_samples

		except NameError:
			pass


	#Method that actually acquires the data. The resulting array is (_channel_no x n_samples) (so n_samples per photodetctor).
	def acquire_data(self):
		self.acq_data=self.dq_task.read(number_of_samples_per_channel=self.n_samples)



#################################################################################################################



"""
Class that takes care of reading data from photodetectors through the DAQ to measure power of the doubled laser. 
It initializes by creating a DAQ Task and by adding channel for the first science laser.
"""
class Power_PD_task:

	def __init__(self,dev,name):
		self.dq_task=dq.Task(new_task_name=name)
		self.device=dev
		self.acq_data=[]
		self.power=[]
		self.n_samples=10

		#Eventually equal to number of slave lasers.
		self._channel_no=0


	#Starting the task. Reading data is usually not started automatically.
	def start(self):
		self.dq_task.start()


	def stop(self):
		self.dq_task.stop()


	#Adds an analog input channel connected to the photodetector that is associated with one of the slave lasers.
	def add_laser(self,channel):
		self.dq_task.ai_channels.add_ai_voltage_chan(self.device.name+"/ai"+str(channel))
		self._channel_no+=1
		self.power.append(deque(maxlen=40))
		self.power[-1].append(0)


	#Method that actually acquires the data. The resulting array is (_channel_no x n_samples) (so n_samples per photodetctor).
	def acquire_data(self,sim):
		if self._channel_no>1:
			self.acq_data=self.dq_task.read(number_of_samples_per_channel=self.n_samples)	
		else:
			self.acq_data=[self.dq_task.read(number_of_samples_per_channel=self.n_samples)]

		if sim:
			self.acq_data=[[242+random.random() for i in range(self.n_samples)] for j in range(self._channel_no)]

		for i in range(self._channel_no):
			self.power[i].append(math.sqrt(sum([x**2 for x in self.acq_data[i]])/self.n_samples))

		



#################################################################################################################


"""
The global function is defined to simply setup tasks using information from the config file. This function is run inside the GUI initialization
when a TransferLock obejct is initialized. This method simply creates a DAQ_tasks object, adds references to Scan, L_task and PD_task objects,
adjusts parameters and sets up and synchronises clocks. It returns object of the DAQ_tasks class.
"""
def setup_tasks(cfg,n,simulate):

	if cfg['DAQ']['DeviceName']=="default":
		tq=DAQ_tasks(simulate)
	else:
		tq=DAQ_tasks(simulate,dev_name=cfg['DAQ']['DeviceName'])

	tq.set_scan_task("Scan",channel=int(cfg['CAVITY']['OutputChannel']))
	tq.set_laser_task("Lasers")
	tq.set_PD_task("PDs",scan_channel=int(cfg['CAVITY']['InputChannel']))
	tq.set_power_task("Power")
	tq.setup_scanning(float(cfg['CAVITY']['MinVoltage']),float(cfg['CAVITY']['MaxVoltage']),float(cfg['CAVITY']['ScanOffset']),float(cfg['CAVITY']['ScanAmplitude']),int(cfg['CAVITY']['ScanSamples']),int(cfg['CAVITY']['ScanTime'])) 
	tq.add_laser(int(cfg['LASER1']['InputChannel']),int(cfg['LASER1']['OutputChannel']),int(cfg['LASER1']['PowerChannel']))
	if n>1:
		tq.add_laser(int(cfg['LASER2']['InputChannel']),int(cfg['LASER2']['OutputChannel']),int(cfg['LASER2']['PowerChannel']))
		tq.set_laser_voltage_boundaries([float(cfg['LASER1']['MinVoltage']),float(cfg['LASER2']['MinVoltage'])],[float(cfg['LASER1']['MaxVoltage']),float(cfg['LASER2']['MaxVoltage'])])
		tq.set_laser_volts([float(cfg['LASER1']['SetVoltage']),float(cfg['LASER2']['SetVoltage'])])
	else:
		tq.set_laser_voltage_boundaries([float(cfg['LASER1']['MinVoltage'])],[float(cfg['LASER1']['MaxVoltage'])])
		tq.set_laser_volts([float(cfg['LASER1']['SetVoltage'])])
	
	tq.set_input_timing()



	return tq


#Helper function.
def channel_number(channel):
	try:
		x=int(channel[-2:])
	except:
		x=int(channel[-1])

	return x

def generate_data(A,B,G,N,start,end):

	X=np.linspace(start,end,num=N)

	Y=[lor(X[i],A,B,G) for i in range(len(X))]

	return Y

def add_noise(data,var):

	noise=var*np.random.randn(len(data))

	return data+noise

def lor(x,A,B,G):
	res=0
	for i in range(len(A)):
		res+=A[i]/(G[i]**2+(x-B[i])**2)
	return res