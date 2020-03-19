import nidaqmx as dq
import matplotlib.pyplot as plt
import numpy as np
import math
import queue
import logging
import h5py
from threading import Thread, Event
from time import sleep
from scipy.optimize import curve_fit

from .DAQ_tasks import *
from .Lock import *
from .Themes import Colors

"""
This file contains the class that represents the transfer lock and two helper classes. The main class ("TransferLock")
uses the class "Lock" to generate feedback signal and applires it to devices by communicating with the DAQ through
the DAQ_tasks class contained in a different file. This class is responsible for acquiring the signal and filtering
(through helper classes), extracting necessary information, updating the GUI, obtaining the mentioned feedback,
applying it through DAQ and plotting the data. In summary, it manages the cavity scan and its data.

"""

#We might need to log some things directly.
log=logging.getLogger(__name__)


"""
The method in this class that's directly run from GUI is the "start_scan" method. Other methods are run through
the scan function and are called only if locks are engaged. This class also has conntainers for error signals'
history (in a queue) that is used for plotting.
"""
class TransferLock:

	def __init__(self,lock,tasks,cfg):

		n=len(lock.slave_lockpoints)

		self.lock=lock    			#Lock object
		self.filter=Filter()		#Filter object
		self.daq_tasks=tasks    	#DAQ_tasks object
		self.master_signal=0    	#Full acquired signal
		self.slave_signals=[0]*n

		self._err_data_length=100   #Length of the error signal collected

		self.rms_points=min(int(lock.cfg['CAVITY']['RMS']),self._err_data_length) #Number of points used for RMS calculation

		#This variable defines the limit below which master laser is considered locked.
		self.master_rms_crit=float(cfg['CAVITY']['LockThreshold']) #ms

		#Lock flags
		self.master_lock_engaged=False
		self.master_locked_flag=False

		#Error signal history is contained in the queue (kept in MHz)
		self.master_err_history=deque(maxlen=self._err_data_length)
		self.master_err_history.append(0)

		#Current RMS of the error signal
		self.master_err_rms=0

		#Criterion used for peak finding
		self.master_peak_crit=float(cfg['CAVITY']['PeakCriterion'])

		#RMS criteria for slave lasers
		self.slave_rms_crits=[float(cfg['LASER1']['LockThreshold'])]
		if n>1:
			self.slave_rms_crits.append(float(cfg['LASER2']['LockThreshold'])) #MHz

		#Peak finding criteria for slave lasers
		self.slave_peak_crits=[float(cfg['LASER1']['PeakCriterion'])]
		if n>1:
			self.slave_peak_crits.append(float(cfg['LASER2']['PeakCriterion']))

		#Flags in form of threading.Event (necessary for frequency sweep)
		self.slave_locked_flags=[Event()]
		if n>1:
			self.slave_locked_flags.append(Event())

		#RMS history
		self.slave_err_history=[deque(maxlen=self._err_data_length)]
		if n>1:
			self.slave_err_history.append(deque(maxlen=self._err_data_length)) #Kept in MHz instead of r

		for i in range(n):
			self.slave_err_history[i].append(0)

		#Current RMS (in MHz as well)
		self.slave_err_rms=[0]*n

		"""
		Lock counter. For slave lasers, they are considered locked if their error signal RMS is below the threshold
		50 consecutive times (can be changed).
		"""
		self.slave_lock_counters=[0]*n
		self._slave_lock_count=50

		#Flags
		self.slave_locks_engaged=[False]*n

		#Queue to calculate average real scanning frequency
		self._scan_frequency=deque(maxlen=10)

		#Counter for number of times scan was performed (used when logging turned on) before being paused.
		self._counter=0
		self._master_counter=0
		self._slave_counters=[0,0]

		#Helpful flags and events
		self._scan_thread=None
		self._scan_flag=False
		self._scan_finished=Event()
		self._scan_paused=Event()
		self._lck_adjust_fin=Event()
		self._slck_adjust_fin=[]
		for i in range(n):
			self._slck_adjust_fin.append(Event())

	#Flag changes
	def start_scan(self):
		self._scan_flag=True


	def stop_scan(self):
		self._scan_flag=False

	"""
	Two function below are responsible for "acquiring" signal, by which I mean filtering the signal and finding
	peaks. The data is in reality obtained regardless of these functions and is contained in DAQ_tasks object,
	which is used here as arguments of initialization for Signal class object. These functions are run only
	if appropriate locks are engaged.
	"""
	def obtain_master_signal(self):
		try:
			self.master_signal=Signal(self.daq_tasks.time_samples,self.daq_tasks.PD_data[0],self.filter)
			self.master_signal.find_peaks(criterion=self.master_peak_crit,win_size=(self.daq_tasks.ao_scan.n_samples//400))
		except Exception as e:
			log.warning(e)


	def obtain_slave_signal(self,ind):
		try:
			self.slave_signals[ind]=Signal(self.daq_tasks.time_samples,self.daq_tasks.PD_data[ind+1],self.filter)
			self.slave_signals[ind].find_peaks(criterion=self.slave_peak_crits[ind],win_size=(self.daq_tasks.ao_scan.n_samples//400))
		except Exception as e:
			log.warning(e)


	"""
	Series of locking functions that are used only if appropriate locks are engaged and if master signal has exactly
	2 peaks. The flags (in form of threading.Event) are used to time different processes correctly. They're just a
	safety precaution.

	Both lock (lock_master and lock_laser) functions first call a different method, which refreshes the lock. These
	functions (refresh_master_lock and refresh_slave_lock) first call a function from the Lock class that uses
	the filtered signal and previously found peak positions (through obtain_master(slave)_signal method) contained
	in the object of Signal class that's saved to one of this object's attributes. The Lock class method finds new
	errors for this iterations and returns them ("mer" and "ser" variables below).

	These errors are then passed to update_master_error and update_slave_error methods. These simply add the error
	to appropriate queues and then calculate RMS of the error signal using chosen number of points. The resulting
	RMS is compared with thresholds and status of the laser is changed to "locked", if criterion is met. For slave
	laser the criterion has to be met for 25 consecutive iterations to considered the laser locked.

	Finally, a method of the Lock class is called and it calculates the feedback signals using gains and current
	and previous error signals. Once this is done, for the cavity lock the scanning offset is moved by amount set
	by the feedback signal, and for lasers their voltages are adjusted (moved) by amounts set by their respective
	feedback signals.
	"""

	def lock_master(self):

		self._lck_adjust_fin.clear()

		self.refresh_master_lock()
		self.daq_tasks.ao_scan.move_offset(self.lock.master_ctrl)

		self._lck_adjust_fin.set()


	def lock_laser(self,ind):

		self._slck_adjust_fin[ind].clear()

		self.refresh_slave_lock(ind)

		voltages=self.daq_tasks.ao_laser.voltages

		voltages[ind]+=self.lock.slave_ctrls[ind]

		self.daq_tasks.set_laser_volts(voltages)

		self._slck_adjust_fin[ind].set()


	def refresh_master_lock(self):

		mer=self.lock.acquire_master_signal(self.master_signal)
		self.update_master_error(mer)
		self.lock.refresh_master_control()


	def refresh_slave_lock(self,ind):

		ser=self.lock.acquire_slave_signal(self.slave_signals[ind],ind)
		self.update_slave_error(ser,ind)
		self.lock.refresh_slave_control(ind)


	def update_master_error(self,err):

		#It is a FIFO queue which automatically removes the oldest element if it becomes over limit
		self.master_err_history.append(err)

		if len(self.master_err_history)<=self.rms_points:
			self.master_err_rms=math.sqrt(np.sum(np.power(list(self.master_err_history),2))/len(list(self.master_err_history)))
		else:
			self.master_err_rms=math.sqrt(np.sum(np.power(list(self.master_err_history)[-self.rms_points:],2))/self.rms_points)

		if self.master_err_rms<self.master_rms_crit:
			self.master_locked_flag=True
		else:
			self.master_locked_flag=False


	def update_slave_error(self,err,ind):

		self.slave_err_history[ind].append(err)
		if len(self.slave_err_history[ind])<=self.rms_points:
			self.slave_err_rms[ind]=math.sqrt(np.sum(np.power(list(self.slave_err_history[ind]),2))/len(list(self.slave_err_history[ind])))
		else:
			self.slave_err_rms[ind]=math.sqrt(np.sum(np.power(list(self.slave_err_history[ind])[-self.rms_points:],2))/self.rms_points)

		if self.slave_err_rms[ind]<self.slave_rms_crits[ind]:
			self.slave_lock_counters[ind]+=1
			if self.slave_lock_counters[ind]>self._slave_lock_count:
				self.slave_locked_flags[ind].set()
			else:
				self.slave_locked_flags[ind].clear()
		else:
			self.slave_lock_counters[ind]=0
			self.slave_locked_flags[ind].clear()



	def master_logging_loop(self,GUI_object=None):


		while GUI_object.master_logging_flag.is_set():

			sleep(10)

			if GUI_object.master_error_temp.empty():
					continue

			with h5py.File(GUI_object.mlog_filename,'a') as f:

				queue_length=len(list(GUI_object.master_error_temp.queue))

				dataset_length=f['Errors'].shape[0]

				if dataset_length==1:
					f['Errors'].resize(queue_length,axis=0)
					f['Time'].resize(queue_length,axis=0)
				else:
					f['Errors'].resize(dataset_length+queue_length,axis=0)
					f['Time'].resize(dataset_length+queue_length,axis=0)

				f['Errors'][-queue_length:]=list(GUI_object.master_error_temp.queue)
				f['Time'][-queue_length:]=list(GUI_object.master_time_temp.queue)

				GUI_object.master_error_temp=queue.Queue(maxsize=10000)
				GUI_object.master_time_temp=queue.Queue(maxsize=10000)




	def slave_logging_loop(self,GUI_object=None,ind=None):


		while GUI_object.slave_logging_flag[ind].is_set():
			sleep(10)


			if GUI_object.slave_err_temp[ind].empty():
				continue

			with h5py.File(GUI_object.laslog_filenames[ind],'a') as f:

				queue_length=len(list(GUI_object.slave_err_temp[ind].queue))
				dataset_length=f['Errors'].shape[0]


				if dataset_length==1:
					f['Errors'].resize(queue_length,axis=0)
					f['Time'].resize(queue_length,axis=0)
					f['RealFrequency'].resize(queue_length,axis=0)
					f['LockFrequency'].resize(queue_length,axis=0)
					f['RealR'].resize(queue_length,axis=0)
					f['LockR'].resize(queue_length,axis=0)
					f['Power'].resize(queue_length,axis=0)
					f['WvmFrequency'].resize(queue_length,axis=0)

				else:
					f['Errors'].resize(dataset_length+queue_length,axis=0)
					f['Time'].resize(dataset_length+queue_length,axis=0)
					f['RealFrequency'].resize(dataset_length+queue_length,axis=0)
					f['LockFrequency'].resize(dataset_length+queue_length,axis=0)
					f['RealR'].resize(dataset_length+queue_length,axis=0)
					f['LockR'].resize(dataset_length+queue_length,axis=0)
					f['Power'].resize(dataset_length+queue_length,axis=0)
					f['WvmFrequency'].resize(dataset_length+queue_length,axis=0)

				try:
					f['Errors'][-queue_length:]=list(GUI_object.slave_err_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_err_temp[ind].queue))
					f['Errors'][-ql:]=list(GUI_object.slave_err_temp[ind].queue)

				try:
					f['Time'][-queue_length:]=list(GUI_object.slave_time_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_time_temp[ind].queue))
					f['Time'][-ql:]=list(GUI_object.slave_time_temp[ind].queue)

				try:
					f['RealFrequency'][-queue_length:]=list(GUI_object.slave_rfreq_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_rfreq_temp[ind].queue))
					f['RealFrequency'][-ql:]=list(GUI_object.slave_rfreq_temp[ind].queue)

				try:
					f['LockFrequency'][-queue_length:]=list(GUI_object.slave_lfreq_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_lfreq_temp[ind].queue))
					f['LockFrequency'][-ql:]=list(GUI_object.slave_lfreq_temp[ind].queue)

				try:
					f['RealR'][-queue_length:]=list(GUI_object.slave_rr_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_rr_temp[ind].queue))
					f['RealR'][-ql:]=list(GUI_object.slave_rr_temp[ind].queue)

				try:
					f['LockR'][-queue_length:]=list(GUI_object.slave_lr_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_lr_temp[ind].queue))
					f['LockR'][-ql:]=list(GUI_object.slave_lr_temp[ind].queue)

				try:
					f['Power'][-queue_length:]=list(GUI_object.slave_pow_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_pow_temp[ind].queue))
					f['Power'][-ql:]=list(GUI_object.slave_pow_temp[ind].queue)

				try:
					f['WvmFrequency'][-queue_length:]=list(GUI_object.slave_wvmfreq_temp[ind].queue)
				except TypeError:
					ql=len(list(GUI_object.slave_wvmfreq_temp[ind].queue))
					f['WvmFrequency'][-ql:]=list(GUI_object.slave_wvmfreq_temp[ind].queue)

				GUI_object.slave_err_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_time_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_rfreq_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_lfreq_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_rr_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_lr_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_pow_temp[ind]=queue.Queue(maxsize=10000)
				GUI_object.slave_wvmfreq_temp[ind]=queue.Queue(maxsize=10000)




	"""
	The function below manages the scan and performs it through the DAQ_tasks class methods. It is run in
	a separate thread that is open from the level of GUI. This function runs as long as the scan flag is
	set to True. Currently, acquiring data and updating the plots is done sequentially in this function.
	The order is as follows:
		- scan is performed, i.e. cavity's piezo is ramped and data from photodetectors acquired
		- time of that task is measured and added to the queue used for calculating real scanning frequency
		- label in the GUI is updated
		- 2D lines on the plot are updated (and redrawn at the end of the function) and axes limits adjusted.
		This refers to the plot showing the data from photodiodes, not the error signal.
		- next, if the cavity lock is not engaged, nothing happens (graphs are just told to redraw) and next
		iteration begins
		- the signal from the master signal is analyzed (peaks are found)
		- if there are not exactly 2 peaks, nothing more happens and next iteration begins
		- otherwise first a GUI element is changed and the locking function called (described above), after
		which, if the master laser/cavity is locked, other GUI elements are updated
		- now whether or not the cavity is locked, the plotting of error signal happens, as well as logging
		to a container, if the user chose to record the error signal; that occurs at the very end of the iteration
		- if the cavity is locked, before error signal plotting happens, the slave lasers are locked, if the
		locks are engaged of course; GUI elements are updated as well

	"""

	def scan(self,GUI_object=None):

		self._scan_paused.clear()
		self._counter=0

		while self._scan_flag:

			self._scan_finished.clear()

			ts=time()

			self.daq_tasks.scan_and_acquire(self._scan_finished)

			self._scan_finished.wait()
			self._scan_frequency.append(1/(time()-ts))

			GUI_object.real_scfr.config(text='{:.1f}'.format(np.mean(list(self._scan_frequency))))

			for i in range(len(self.daq_tasks.PD_data)):
				GUI_object.plot_win.all_lines[i].set_data(self.daq_tasks.time_samples,self.daq_tasks.PD_data[i])
				## plotting smoothed and derivative of master peak
				# try:
				# 	GUI_object.plot_win.all_lines[1].set_data(self.daq_tasks.time_samples,self.master_signal.smooth_der)
				# 	GUI_object.plot_win.all_lines[2].set_data(self.daq_tasks.time_samples,self.master_signal.smooth_y)
				# except:
				# 	pass
				if i==0:
					GUI_object.plot_win.all_lines[i+3].set_data([self.lock.master_lockpoint]*2,[-10,10])
				else:
					GUI_object.plot_win.all_lines[i+3].set_data([self.lock.slave_lockpoints[i-1]*self.lock.interval+self.lock.master_lockpoint]*2,[-10,10])
			GUI_object.plot_win.ax.set_xlim(self.daq_tasks.ao_scan.scan_time*0.2, self.daq_tasks.ao_scan.scan_time*1.01)
			GUI_object.plot_win.ax.set_ylim(np.amin(self.daq_tasks.PD_data)-0.05, np.amax(self.daq_tasks.PD_data)+0.2)

			if self.master_lock_engaged:

				self.obtain_master_signal()

				if len(self.master_signal.peaks_x)==2:

					GUI_object.twopeak_status_cv.itemconfig(GUI_object.twopeak_status,fill=Colors['on_color'])

					self.lock_master()

					self._lck_adjust_fin.wait()

					GUI_object.rms_cav.config(text="{:.3f}".format(self.master_err_rms))
					GUI_object.real_scoff.config(text='{:.2f}'.format(self.daq_tasks.ao_scan.offset))
				else:
					GUI_object.twopeak_status_cv.itemconfig(GUI_object.twopeak_status,fill=Colors['off_color'])

				if self.master_locked_flag:
					GUI_object.networkio.master_locked_flag = True
					GUI_object.cav_lock_status_cv.itemconfig(GUI_object.cav_lock_status,fill=Colors['on_color'])

					if any(self.slave_locks_engaged):
						for i in range(len(self.slave_locks_engaged)):
							if self.slave_locks_engaged[i]:
								self.obtain_slave_signal(i)
								self.lock_laser(i)
								self._slck_adjust_fin[i].wait()

								GUI_object.rms_laser[i].config(text="{:.2f}".format(self.slave_err_rms[i]))
								GUI_object.app_volt[i].config(text='{:.3f}'.format(self.daq_tasks.ao_laser.voltages[i]))
								GUI_object.laser_r[i].config(text='{:.3f}'.format(GUI_object.lock.slave_Rs[i]))

								if self.slave_locked_flags[i].is_set():
									GUI_object.laser_lock_status_cv[i].itemconfig(GUI_object.laser_lock_status[i],fill=Colors['on_color'])
								else:
									GUI_object.laser_lock_status_cv[i].itemconfig(GUI_object.laser_lock_status[i],fill=Colors['off_color'])
				else:
					GUI_object.networkio.master_locked_flag = False
					GUI_object.networkio.master_err = np.nan
					GUI_object.cav_lock_status_cv.itemconfig(GUI_object.cav_lock_status,fill=Colors['off_color'])
					for j in range(len(self.slave_locks_engaged)):
						GUI_object.networkio.slave_locked_flags[j] = False
						GUI_object.networkio.slave_err[j] = np.nan
						GUI_object.networkio.slave_frequency[j] = np.nan
						GUI_object.networkio.slave_lockpoint[j] = GUI_object.lock.get_laser_abs_lockpoint(j)

			else:
				GUI_object.networkio.master_locked_flag = False
				GUI_object.networkio.master_err = np.nan
				for j in range(len(self.slave_locks_engaged)):
					GUI_object.networkio.slave_locked_flags[j] = False
					GUI_object.networkio.slave_err[j] = np.nan
					GUI_object.networkio.slave_frequency[j] = np.nan
					GUI_object.networkio.slave_lockpoint[j] = GUI_object.lock.get_laser_abs_lockpoint(j)

			if self.master_lock_engaged and len(self.master_signal.peaks_x)==2:

				X=np.linspace(0,len(self.master_err_history)-1,len(self.master_err_history))
				GUI_object.plot_win.mline.set_data(X,self.master_err_history)
				GUI_object.plot_win.ax_err.set_ylim(min(self.master_err_history)-self.master_rms_crit/3, self.master_rms_crit/3+max(self.master_err_history))
				GUI_object.plot_win.ax_err.set_xlim(min(X), max(X))

				GUI_object.networkio.master_err = self.master_err_history[-1]

				if GUI_object.master_logging_set:

					GUI_object.master_time_temp.put(time()-GUI_object.mt_start)
					GUI_object.master_error_temp.put(GUI_object.lock.master_err)

					self._master_counter+=1

				for j in range(len(self.slave_locks_engaged)):
					if self.slave_locks_engaged[j]:
						GUI_object.networkio.slave_locked_flags[j] = True
						Xs=np.linspace(0,len(self.slave_err_history[j])-1,len(self.slave_err_history[j]))
						GUI_object.plot_win.slines[j].set_data(Xs,self.slave_err_history[j])
						try:
							GUI_object.plot_win.ax_err_L[j].set_ylim(min(self.slave_err_history[j])-self.slave_rms_crits[j]/3, self.slave_rms_crits[j]/3+max(self.slave_err_history[j]))
						except:
							pass
						try:
							GUI_object.plot_win.ax_err_L[j].set_xlim(min(Xs), max(Xs))
						except:
							pass

						GUI_object.networkio.slave_err[j] = self.slave_err_history[j][-1]
						GUI_object.networkio.slave_frequency[j] = GUI_object.lock.get_laser_abs_freq(j)
						GUI_object.networkio.slave_lockpoint[j] = GUI_object.lock.get_laser_abs_lockpoint(j)

						if GUI_object.laser_logging_set[j]:


							GUI_object.slave_time_temp[j].put(time()-GUI_object.lt_start[j])
							GUI_object.slave_err_temp[j].put(self.slave_err_history[j][-1])
							GUI_object.slave_rfreq_temp[j].put(GUI_object.lock.get_laser_abs_freq(j))
							GUI_object.slave_lfreq_temp[j].put(GUI_object.lock.get_laser_abs_lockpoint(j))
							GUI_object.slave_rr_temp[j].put(GUI_object.lock.slave_Rs[j])
							GUI_object.slave_lr_temp[j].put(GUI_object.lock.slave_lockpoints[j])
							GUI_object.slave_pow_temp[j].put(1000*np.mean(self.daq_tasks.power_PDs.power[j]))
							GUI_object.slave_wvmfreq_temp[j].put(GUI_object.real_frequency[j][0])


							self._slave_counters[j]+=1
					else:
						GUI_object.networkio.slave_locked_flags[j] = False
						GUI_object.networkio.slave_err[j] = np.nan
						GUI_object.networkio.slave_frequency[j] = np.nan
						GUI_object.networkio.slave_lockpoint[j] = GUI_object.lock.get_laser_abs_lockpoint(j)
			else:
				for j in range(len(self.slave_locks_engaged)):
					GUI_object.networkio.slave_locked_flags[j] = False
					GUI_object.networkio.slave_err[j] = np.nan
					GUI_object.networkio.slave_frequency[j] = np.nan
					GUI_object.networkio.slave_lockpoint[j] = GUI_object.lock.get_laser_abs_lockpoint(j)

			self._counter+=1


			GUI_object.plot_win.fig.canvas.draw_idle()

		self._scan_paused.set()


#################################################################################################################

"""
Class below is responsible for smoothing the data, taking the derivative and finding peaks. I have found by trial
and error that the smallest error of peak finding happens for the parameters that are used as default and for
the procedure used to find them. The peak finding algorithm takes approxiamtely 0.6ms, so it is no way a bottleneck.
"""

class Signal:

	"""
	To initialize an object of this class one needs the X and Y data and an object of Filter class. During the
	initialization the data is smoothed using an SG filter.
	"""
	def __init__(self,datax,datay,fltr):

		self.data_x=datax
		self.data_y=datay-np.mean(datay[int(len(datay)/5):])
		self.dx=datax[1]-datax[0]
		self.mx=np.max(self.data_y[int(0.25*len(datay)):])
		# self.smooth_y=fltr.apply(datay,0,datax[1]-datax[0])
		self.smooth_y=fltr.peak_filter(self.data_y)
		self.fltr=fltr
		self.der_y=[]
		self.smooth_der=[]
		self.peaks_x=[]
		self.peaks_y=[]


	"""
	To find the peaks we look at the zero crossing of the derivative signal. The algorithm first finds first derivative
	of the signal. Then, because taking a derivative a noise-amplifying process, the derivative signal is smoothed using
	SG filter and then using a moving average.

	To find peaks, we go over smoothed derivative signal until we find 2 points that are on the opposite side of 0 (we're
	looking for them to also have a positive slope). Once two such points are found, the algorithm looks at the hight of
	the peak in the data. It is considered a real peak if peak>criterion*max(data). Because in our measurement we're going
	to observe one or two peaks of similar height, with a decent SNR, such a simple criterion works perfectly well.

	To find the position of the peak, we fit a linear function to 14 points around the zero crossing and get the zero
	crossing from the fit. 14 points used for a fit works very well for 1000 points per scan and peaks that are not extremely
	narrow. This can be changed if necessary. Once the peak is found, the loop is skipped by "win_size".
	"""
	def find_peaks(self,criterion=0.25,win_size=5,hs=10):

		D=self.fltr.apply(self.smooth_y,1,self.dx)
		self.der_y=self.fltr.apply(self.data_y,1,self.dx)
		# D=self.fltr.apply(D,0,self.dx)
		D=self.fltr.moving_avg(D,half_size=2)
		self.smooth_der=D

		points=[]
		skip=0

		#We discard/ignore first 25% of the data. Real scan introduces terrible noise there.
		for i in range(int(0.25*len(D)),len(D)-win_size):

			if skip>0:
				skip-=1
				continue

			if D[i-1]<0 and D[i]>0:

				if np.amax(self.data_y[i-win_size:i+win_size])>criterion*self.mx:
					a,b=np.polyfit(self.data_x[i-win_size:i+win_size],D[i-win_size:i+win_size],1)
					points.append(-b/a)

					skip=20*win_size

		if len(points)==0:
			D=self.der_y
			skip=0
			for i in range(int(0.25*len(D)),len(D)-win_size):

				if skip>0:
					skip-=1
					continue

				if D[i-1]<0 and D[i]>0:

					if np.amax(self.data_y[i-win_size:i+win_size])>criterion*self.mx:

						a,b=np.polyfit(self.data_x[i-win_size:i+win_size],D[i-win_size:i+win_size],1)
						points.append(-b/a)
						skip=20*win_size

		self.peaks_x=np.array(points)


	#Function that finds interpolated values at the found peak position.
	def get_ypeaks(self):

		if len(self.peaks_x)==0:
			return

		f=interpolate.interp1d(self.data_x,self.smooth_y)

		self.peaks_y=f(self.peaks_x)




#################################################################################################################


"""
A helper class that defines multiple SG filters and a moving average. In the coefficent array, the first element is
a smoothing window, the second one is first derivative, the third one is second derivative, and the last element can
be used to obtained thrid derivative of the signal. These windows are convolved with the signal to obtain desired
result. Finally, moving average is defined, which is a convolution with a special [1,1,...,1]/n window.
"""
class Filter:

	def __init__(self):

		self.coeffs=[[-2/21,3/21,6/21,7/21,6/21,3/21,-2/21],[-3/10,-1/5,-1/10,0,1/10,1/5,3/10],[5/42,0,-3/42,-4/42,-3/42,0,5/42],[-1/6,1/6,1/6,0,-1/6,-1/6,1/6]]

	def apply(self,signal,der,sp): #Make it more efficient with np.convolve!

		C=self.coeffs[der][:]
		if der>1:
			C=[c/sp**der for c in C]

		return np.convolve(signal,C,"same")

	def moving_avg(self,data,half_size=3):

		return np.divide(np.convolve(data,np.ones(2*half_size+1),"same"),2*half_size+1)


	def peak_filter(self,data,k=10):
		return np.concatenate((np.concatenate((data[:k],[data[i]**2-data[i-k]*data[i+k] for i in range(k,len(data)-k)])),data[-k:]))
