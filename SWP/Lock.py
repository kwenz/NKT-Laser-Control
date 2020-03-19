import numpy as np
import matplotlib.pyplot as plt
from collections import deque
from scipy import interpolate
from statistics import mean, stdev
from time import sleep, time
import random
import sys
import math
import threading
import logging



"""
The class in this file represents the locking mechanism and feedback for the cavity and both slave lasers.
It also keeps values for the lockpoints, FSRs and frequencies used to obtain those parameters.

"""

class Lock:

	def __init__(self,wvls,cfg):

		self.cfg=cfg

		self.master_lockpoint=float(cfg['CAVITY']['Lockpoint'])

		#Internally, lockpoints of slave lasers are kept in form of the R parameter.
		self.slave_lockpoints=[float(cfg['LASER1']['LockpointR'])]
		if len(wvls)>1:
			self.slave_lockpoints.append(float(cfg['LASER2']['LockpointR']))

		#The 0 MHz lockpoint is chosen to be at R=0.5
		self.zero_slave_lockpoints=[0.5]*len(wvls)

		#Errors
		self.master_err=0
		self.master_err_prev=0
		self.slave_errs=[0]*len(wvls)
		self.slave_errs_prev=[0]*len(wvls)

		self._wrong_peak_counter=[0,0]

		"""
		Slave lasers' R parameters. They are defined as the ratio of the interval between the difference of
		positions of peaks of the slave and master lasers and two peaks of the master laser. In other words,
		if:
			t1 - position (time of arrival) of the first peak of the master laser
			t2 - position (time of arrival) of the second peak of the master laser
			ts - position (time of arrival) of the peak of the slave laser
		then:
			R= (ts-t1)/(t2-t1)
		This parameter can be negative or bigeer than 1.
		"""
		self.slave_Rs=[0]*len(wvls)
		self.slave_sectors=[0]*len(wvls)

		#Control (feedback) signals
		self.master_ctrl=0
		self.slave_ctrls=[0]*len(wvls)

		#Peak positions
		self.master_peaks=[]
		self.slave_peaks=[0]*len(wvls)
		self.prev_slave_peaks=[0]*len(wvls)

		#Gains. This program uses PI loops.
		self.prop_gain=[float(cfg['CAVITY']['PGain']),float(cfg['LASER1']['PGain'])]
		if len(wvls)>1:
			self.prop_gain.append(float(cfg['LASER2']['PGain']))
		self.int_gain=[float(cfg['CAVITY']['IGain']),float(cfg['LASER1']['IGain'])]
		if len(wvls)>1:
			self.int_gain.append(float(cfg['LASER2']['IGain']))

		#Interval between master peaks (t2-t1)
		self.interval=0 #ms

		#Frequency of slave lasers that are used to calculate adjusted FSRs. Doesn't have to be too precise.
		self.slave_freqs=[0]*len(wvls)


		#Initially chosen frequencies
		self._def_slave_freqs=[0]*len(wvls)
		#Cavity's FSR
		self._FSR=float(cfg['CAVITY']['FSR']) #GHz
		#Frequency of the master laser.
		self._master_freq=0 #GHz
		#Adjusted FSRs for the slave lasers
		self._slave_FSR=[0]*len(wvls)


		self.set_master_frequency(float(cfg['CAVITY']['Wavelength']))
		self._initialize_slave_freqs(wvls)


	#Couple of self-explanatory methods.
	def set_master_frequency(self,wavelength):
		c=299792458
		self._master_freq=c/wavelength


	def set_slave_frequency(self,wavelength,ind):
		self.slave_freqs[ind]=299792458/wavelength
		self.update_slave_FSRs()  #Slave FSRs depend on slave lasers' waavelengths


	def get_master_wavelength(self):
		return 299792458/self._master_freq


	def get_slave_wavelength(self,ind):
		return 299792458/self.slave_freqs[ind]


	def set_FSR(self,FSR):
		if FSR<=0:
			FSR=1000
		self._FSR=FSR/1000   #Users provide it in MHz; it is kept here in GHz


	def _initialize_slave_freqs(self,wavelengths):
		c=299792458
		for i in range(len(wavelengths)):
			self.slave_freqs[i]=c/wavelengths[i]
			if self._def_slave_freqs[i]==0:
				self._def_slave_freqs[i]=c/wavelengths[i]
		self.update_slave_FSRs()


	"""
	To obtain slave laser's FSR one has to multiply cavity's FSR (defined at the master laser frequency)
	by the ratio of frequencies, i.e.:
		SlaveFSR = CavityFSR * f_slave/f_master
	"""
	def update_slave_FSRs(self):
		for i in range(len(self.slave_freqs)):
			self._slave_FSR[i]=(self.slave_freqs[i])*self._FSR/self._master_freq


	def set_master_lockpoint(self,lp):
		self.master_lockpoint=lp


	def move_master_lockpoint(self,lp):
		self.master_lockpoint+=lp


	#User provides deviation in MHz, which has to be translated into units of R using slave laser's FSR
	def set_laser_lockpoint(self,deviation,ind):
		# deviation*=-1
		x=1000*self._FSR/2
		if deviation>x:
			self.slave_sectors[ind]=math.ceil((deviation-x)/(2*x))
			deviation-=self.slave_sectors[ind]*2*x
		elif deviation<-x:
			self.slave_sectors[ind]=-math.ceil((-deviation-x)/(2*x))
			deviation-=self.slave_sectors[ind]*2*x
		else:
			self.slave_sectors[ind]=0
		self.slave_lockpoints[ind]=self.zero_slave_lockpoints[ind]-deviation/(1000*self._slave_FSR[ind])


	def move_laser_lockpoint(self,deviation,ind):
		# deviation*=-1
		new_fr=self.get_laser_lockpoint(ind)+deviation
		if new_fr>self._FSR*1000/2:
			new_fr-=self._FSR*1000
			self.slave_lockpoints[ind]=self.zero_slave_lockpoints[ind]-new_fr/(1000*self._slave_FSR[ind])
			self.slave_sectors[ind]+=1

		elif new_fr<-self._FSR*1000/2:
			new_fr+=self._FSR*1000
			self.slave_lockpoints[ind]=self.zero_slave_lockpoints[ind]-new_fr/(1000*self._slave_FSR[ind])
			self.slave_sectors[ind]-=1
		else:
			self.slave_lockpoints[ind]-=deviation/(1000*self._slave_FSR[ind]) #deviation in MHz


	"""
	It is worth mentioning that to translate R unit to MHz, one just has to multiply the deviation from the
	0 point by the FSR, i.e. if:
		lr - current lockpoint in R
		zlr - zero lockpoint in R
		lm - current lockpoint in MHz
		F - slave laser's FSR in GHz
	then:
		lm = -(lr - zlr)*F*1000
	"""
	def get_laser_lockpoint(self,ind):
		return -(self.slave_lockpoints[ind]-self.zero_slave_lockpoints[ind])*self._slave_FSR[ind]*1000


	def get_laser_abs_lockpoint(self,ind):
		return self.slave_sectors[ind]*self._FSR*1000-(self.slave_lockpoints[ind]-self.zero_slave_lockpoints[ind])*self._slave_FSR[ind]*1000


	def get_laser_local_freq(self,ind):
		return -(self.slave_Rs[ind]-self.zero_slave_lockpoints[ind])*self._slave_FSR[ind]*1000


	def get_laser_abs_freq(self,ind):
		return self.slave_sectors[ind]*self._FSR*1000-(self.slave_Rs[ind]-self.zero_slave_lockpoints[ind])*self._slave_FSR[ind]*1000


	def adjust_gains(self,prop,integral):
		if len(prop)!=len(self.prop_gain) or len(integral)!=len(self.int_gain):
			raise ValueError('Please provide all the necessary gains.') #Probably unnecessary. All gains are kept as a list.
			return
		self.prop_gain=prop
		self.int_gain=integral
		self.master_ctrl=0

	"""
	The function below uses as its argument an object of Signal class defined in Data_acq.py file. From that object it simply
	obtains position of peaks to use for error signal calculation. The function returns current error signal.
	"""
	def acquire_master_signal(self,signal):
		#There have to be exactly 2 peaks for the program to work properly. This has to be adjusted by the user by changing scan parameters.
		if len(signal.peaks_x)!=2:
			return
		else:
			#We sort the peaks in increasing order with respect to arrival time.
			self.master_peaks=sorted(signal.peaks_x)

			#Save previously obtained error signal
			self.master_err_prev=self.master_err

			#What we're locking is actually the first peak. The error signal is just distance between peak and the lockpoint (in ms).
			self.master_err=self.master_peaks[0]-self.master_lockpoint

			#We also calculate the interval between the peaks.
			self.interval=(signal.peaks_x[-1]-signal.peaks_x[0])


		return self.master_err/self.interval*self._FSR*1000


	#Analogical function to the previous one. It uses the first detected peak of the slave laser. It returns the error in units of MHz.
	def acquire_slave_signal(self,signal,ind):
		if len(signal.peaks_x)>0:

			prev=self.slave_errs_prev[ind]
			prev_peak=self.prev_slave_peaks[ind]

			self.slave_errs_prev[ind]=self.slave_errs[ind]
			self.prev_slave_peaks[ind]=self.slave_peaks[ind]

			self.slave_peaks[ind]=signal.peaks_x[0]

			#Current R parameter of the slave laser is calculated.
			self.slave_Rs[ind]=(self.master_peaks[0]-self.slave_peaks[ind])/(self.master_peaks[0]-self.master_peaks[1])

			#The error is just the difference between laser's peak and the lockpoint in the units of R.
			self.slave_errs[ind]=self.slave_lockpoints[ind]-self.slave_Rs[ind]


			if len(signal.peaks_x)>1:
				for peak in signal.peaks_x[1:]:
					err=self.slave_lockpoints[ind]-(self.master_peaks[0]-peak)/(self.master_peaks[0]-self.master_peaks[1])
					if abs(err)<abs(self.slave_errs[ind]):
						self.slave_errs[ind]=err
						self.slave_peaks[ind]=peak
						self.slave_Rs[ind]=self.slave_lockpoints[ind]-err

			#If suddenly error jumps to 0.4 FSR, all parameters are returned to previous values. If this happens more than 5 times,
			#it is interpreted as a deliberate movement of the lockpoint and the loop continues.
			if abs(self.slave_errs[ind]-self.slave_errs_prev[ind])*1000*self._slave_FSR[ind]>=0.4*1000*self._FSR and self._wrong_peak_counter[ind]<5:
				self.slave_errs[ind]=self.slave_errs_prev[ind]
				self.slave_peaks[ind]=self.prev_slave_peaks[ind]
				self.slave_errs_prev[ind]=prev
				self.prev_slave_peaks[ind]=prev_peak
				self.slave_Rs[ind]=(self.master_peaks[0]-self.slave_peaks[ind])/(self.master_peaks[0]-self.master_peaks[1])
				self._wrong_peak_counter[ind]+=1
			else:
				self._wrong_peak_counter[ind]=0


			return self.slave_errs[ind]*1000*self._slave_FSR[ind]
		else:
			return self.slave_errs[ind]*1000*self._slave_FSR[ind]


	"""
	Two last methods are the feedback methods that calculate the strength of the control signal (feedback signal) that is later added to
	the voltage that controls either the cavity or frequency of slave lasers. In general the signal is calculated in the following way
	(volecity algorithm):

		Ctrl[i] = Ctrl[i-1] + P*(Err[i] - Err[i-1]) + I*Err[i]*T

	where:
		Ctrl[i] - feedback signal at current i-th iteration
		Ctrl[i-1] - feedback signal at previous (i-1)-th iteration
		P - proportional gain
		I - integral gain
		Err[i] - error signal at current i-th iteration
		Err[i-1] - error signal at previous (i-1)-th iteration
		T - interval between the iterations, or time between the calculation of the error signals. If there is no delay between scanning
		and calculation of the error signel, i.e. if the calculations take almost no time, this is basically scan time. Here, we just
		approximate the scan time with the distance between two peaks of the master signal, which should be at two ends of the scan.
		This can, however, quite easily be changed if necessary.
	The various numerical coeffiicients are there to make the feedback loop work correctly for gains of the order of 1 (so they basically
	rescale the parameters). This can be changed, but once set, it should not be touched.
	"""
	def refresh_master_control(self):
		self.master_ctrl=(self.master_ctrl+0.05*self.prop_gain[0]*(self.master_err-self.master_err_prev)+self.int_gain[0]*self.master_err*self.interval/10000)



	def refresh_slave_control(self,i):
		self.slave_ctrls[i]=self.slave_ctrls[i]+0.05*self.prop_gain[i+1]*(self.slave_errs[i]-self.slave_errs_prev[i])+self.int_gain[i+1]*self.slave_errs[i]*self.interval/10000
