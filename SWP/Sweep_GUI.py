from tkinter import *
from tkinter import messagebox, filedialog
from tkinter import ttk
import matplotlib
import matplotlib.animation as animation
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.gridspec import GridSpec
import threading
import queue
import datetime
import h5py
import logging
import os

from .Config import *
from .Devices import *
from .Data_acq import *



"""
This file contains all GUI classes used in the program. The main class that's first initialized is "GUI" and it 
creates the root window. Once the object is initialized, it can be run. It then creates a 4-element paned 
window: pane 1 is defined by the "LaserConnect" class and contains elements initializing connections with
the hardware, choosing ports and config files; pane 2 defined by class "LaserControl" containes control for the 
NKT Laser (1 tab per laser); pane 3 (class "PlotWindow)containes graphs - error for the master and slave lasers
and the results of cavity scan; pane 4 is created by class "TransferLock" and containes control of the transfer
cavity and the fine control of laser frequency.
"""

class GUI:

	def __init__(self):

		self.root = Tk()

		self.root.title("Laser control")

		self.root.geometry("1820x1000")

		
	def run(self,debug=False,simulate=False):


		pane=PanedWindow(self.root,sashwidth=5,sashpad=2,sashrelief=GROOVE)
		pane.pack(fill=BOTH, expand=1)

		left=Frame(pane,width=200,bd=1)
		pane.add(left)

		r_pane=PanedWindow(sashwidth=5,sashpad=2,sashrelief=GROOVE,orient=VERTICAL)
		r_top=Frame(r_pane,width=610,height=325,bd=4)
		r_pane.add(r_top)
		r_bottom=Frame(r_pane,width=610,height=675,bd=4)
		r_pane.add(r_bottom)
		pane.add(r_pane)

		translock_frame=Frame(pane,width=1010,bd=4)
		pane.add(translock_frame)

		self.ld=LaserConnect(left,r_top,translock_frame,r_bottom,simulate)

		self.root.protocol("WM_DELETE_WINDOW", self.callback)

	# TkInter allows automatic error logging by creating a simple method
		if debug:
			self.log=logging.getLogger(__name__)
			self.root.report_callback_exception=self.log_exception

		self.root.mainloop()

	def log_exception(self,exception,value,traceback):

		self.log.exception(value)
		self.ld.caught_err.config(text="Caught an error:\n"+str(exception)+"\n"+str(value))


	"""
	Function used to close the main window. It might be necessary to delete DAQ tasks before closing
	the window to avoid errors. The laser is also turned off on exit (if on).
	"""
	def callback(self):

		logging.shutdown()

		try:
			del self.ld.TC.transfer_lock.daq_tasks
		except:
			pass
		
		if len(self.ld.laser_tabs)>0:
			for obj in self.ld.laser_tabs:
				if obj.laser.is_on():
					obj.laser.emission_off()
				clr=closePorts(obj.laser.port)
		self.root.destroy()
		import sys
		sys.exit(1)




#################################################################################################################

"""
This class controlls the NKT laser through the class "Laser" in file "Devices.py", which uses DLL from the 
company. This class operates using 3 different threads: the first is just the GUI, second one probes a queue
for updates from the laser regarding its status, power, temperature, wavelength etc. and puts that info on
GUI, the third one probe queue for commands given to the laser from GUI and executes them.

"""
class LaserControl:

	"""
	The class can be initialized with wavelength that was saved in the configuration file. Then, laser's 
	wavelength is set to that value. However, if the default configuration file is chosen at the beginning, 
	this part of the GUI is loaded without the set wavelength (so the laser will have the setting that it's
	currently using).
	"""


	def __init__(self,parent,stat,laser,config_wvl=None):

		self.c=299792.458 

		if laser.is_on():
			laser.emission_off()

		
		self.parent=parent   #Parent window (panes)
		self.status=stat     #Some GUI elements in different pane
		self.laser=laser     #Laser objects

		parent.grid_rowconfigure(0,minsize=2)
		parent.grid_rowconfigure(2,minsize=2)
		parent.grid_rowconfigure(4,minsize=2)

		parent.grid_columnconfigure(0,minsize=5)
		parent.grid_columnconfigure(2,minsize=10)
		parent.grid_columnconfigure(4,minsize=5)
		
		"""
		Min and max wavelengths depend on the laser model, but for us they're limited by approx. 0.37nm both
		ways from the central wavelength of 1086.78nm. 
		"""
		self.max_wv=self.laser.get_central_wavelength()+0.37
		self.min_wv=self.laser.get_central_wavelength()-0.37

		self.exception=None #Container for an exception coming from the laser.


		"""
		This GUI part divides the pane into frames that contain different categories of elements controlling 
		the laser or providing information about it.
		"""

		#Subframe - adjustment window
		self.adjustment_frame=LabelFrame(parent,text="Adjustment")
		self.adjustment_frame.grid(row=1,column=1,sticky=NW)

		self.adjustment_frame.grid_rowconfigure(0,minsize=2)
		self.adjustment_frame.grid_rowconfigure(2,minsize=5)
		self.adjustment_frame.grid_rowconfigure(4,minsize=5)
		self.adjustment_frame.grid_rowconfigure(6,minsize=5)
		self.adjustment_frame.grid_rowconfigure(8,minsize=2)
		self.adjustment_frame.grid_rowconfigure(10,minsize=2)
		self.adjustment_frame.grid_rowconfigure(12,minsize=7)

		self.adjustment_frame.grid_columnconfigure(0,minsize=5)
		self.adjustment_frame.grid_columnconfigure(2,minsize=10)
		self.adjustment_frame.grid_columnconfigure(4,minsize=5)
		self.adjustment_frame.grid_columnconfigure(6,minsize=8)


		Label(self.adjustment_frame,text="Set \u03bb [nm]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.adjustment_frame,text="Set freq. [THz]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.adjustment_frame,text="Move freq. [GHz]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		
		self.new_freq=StringVar()
		self.new_wv=StringVar()

		#Traces are added so that the user can either set wavelength or the frequency. Not both at the same time.
		self.new_wv.trace('w',self.set_wvl_trace) 
		self.new_wv.trace('u',self.set_wvl_trace)
		self.new_freq.trace('w',self.set_wvl_trace)
		self.new_freq.trace('u',self.set_wvl_trace)
		

		self.new_wv_entry=Entry(self.adjustment_frame,textvariable=self.new_wv,width=12)
		self.new_wv_entry.grid(row=3,column=3,columnspan=3)
		self.new_freq_entry=Entry(self.adjustment_frame,textvariable=self.new_freq,width=12)
		self.new_freq_entry.grid(row=5,column=3,columnspan=3)

		"""
		User can also shift the frequency by 1-10 GHz. By the way, all the frequency adjustment that's done 
		using this part of GUI in fact changes temperature of laser's substrate.
		"""
		self.plus1g=Button(self.adjustment_frame,text="+1",width=5,command=lambda: self.move_freq(1),font="Arial 10 bold")
		self.plus1g.grid(row=7,column=5,sticky=E)
		self.plus5g=Button(self.adjustment_frame,text="+5",width=5,command=lambda: self.move_freq(5),font="Arial 10 bold")
		self.plus5g.grid(row=9,column=5,sticky=E)
		self.plus10g=Button(self.adjustment_frame,text="+10",width=5,command=lambda: self.move_freq(10),font="Arial 10 bold")
		self.plus10g.grid(row=11,column=5,sticky=E)
		self.minus1g=Button(self.adjustment_frame,text="-1",width=5,command=lambda: self.move_freq(-1),font="Arial 10 bold")
		self.minus1g.grid(row=7,column=3,sticky=W)
		self.minus5g=Button(self.adjustment_frame,text="-5",width=5,command=lambda: self.move_freq(-5),font="Arial 10 bold")
		self.minus5g.grid(row=9,column=3,sticky=W)
		self.minus10g=Button(self.adjustment_frame,text="-10",width=5,command=lambda: self.move_freq(-10),font="Arial 10 bold")
		self.minus10g.grid(row=11,column=3,sticky=W)

		#Modulation type of the laser can be changed between Narrow and Wide.
		Label(self.adjustment_frame,text="Modulation:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)

		self.mod_var=StringVar()
		self.mod_var_opt=OptionMenu(self.adjustment_frame,self.mod_var,"Wide","Narrow")
		self.mod_var_opt.grid(row=1,column=3,columnspan=3)
		self.mod_var_opt.config(width=8)
		self.mod_var.set(self.laser.get_modulation_type())
		self.mod_var.trace('w',self.change_mod)

		

		#Subframe - constant settings
		self.settings_frame=LabelFrame(parent,text="Settings")
		self.settings_frame.grid(row=3,column=1,columnspan=3,sticky=NW)

		self.settings_frame.grid_rowconfigure(0,minsize=2)
		self.settings_frame.grid_rowconfigure(2,minsize=5)
		self.settings_frame.grid_rowconfigure(4,minsize=2)


		self.settings_frame.grid_columnconfigure(0,minsize=2)
		self.settings_frame.grid_columnconfigure(2,minsize=5)
		self.settings_frame.grid_columnconfigure(4,minsize=5)
		self.settings_frame.grid_columnconfigure(6,minsize=5)
		self.settings_frame.grid_columnconfigure(8,minsize=2)


		Label(self.settings_frame,text="Min. \u03bb:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
		Label(self.settings_frame,text="Max. \u03bb:",font="Arial 10 bold").grid(row=1,column=5,sticky=W)
		Label(self.settings_frame,text="{0:.2f}".format(self.min_wv)+" nm",font="Arial 10").grid(row=1,column=3,sticky=E)
		Label(self.settings_frame,text="{0:.2f}".format(self.max_wv)+" nm",font="Arial 10").grid(row=1,column=7,sticky=E)
		Label(self.settings_frame,text="Max. f:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.settings_frame,text="Min. f:",font="Arial 10 bold").grid(row=3,column=5,sticky=W)
		Label(self.settings_frame,text="{0:.3f}".format(self.c/self.min_wv)+" THz",font="Arial 10").grid(row=3,column=3,sticky=E)
		Label(self.settings_frame,text="{0:.3f}".format(self.c/self.max_wv)+" THz",font="Arial 10").grid(row=3,column=7,sticky=E)



		#Subframe - readout
		self.readout_frame=LabelFrame(parent,text="Readout")
		self.readout_frame.grid(row=1,column=3,sticky=NW)

		self.readout_frame.grid_rowconfigure(0,minsize=2)
		self.readout_frame.grid_rowconfigure(2,minsize=5)
		self.readout_frame.grid_rowconfigure(4,minsize=5)
		self.readout_frame.grid_rowconfigure(6,minsize=5)
		self.readout_frame.grid_rowconfigure(8,minsize=5)
		self.readout_frame.grid_rowconfigure(10,minsize=5)
		self.readout_frame.grid_rowconfigure(12,minsize=5)
		self.readout_frame.grid_rowconfigure(14,minsize=2)


		self.readout_frame.grid_columnconfigure(0,minsize=2)
		self.readout_frame.grid_columnconfigure(2,minsize=5)
		self.readout_frame.grid_columnconfigure(4,minsize=5)
		self.readout_frame.grid_columnconfigure(6,minsize=5)

		Label(self.readout_frame,text="Actual:",font="Arial 10 bold").grid(row=1,column=3)
		Label(self.readout_frame,text="Set:",font="Arial 10 bold").grid(row=1,column=5)
			
		Label(self.readout_frame,text="IR wavelength:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.readout_frame,text="IR frequency:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.readout_frame,text="UV wavelength:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		Label(self.readout_frame,text="UV frequency:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		Label(self.readout_frame,text="Output:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)
		Label(self.readout_frame,text="Temperature:",font="Arial 10 bold").grid(row=13,column=1,sticky=W)

		
		self.set_lam=Label(self.readout_frame,text="",font="Arial 10")
		self.set_lam.grid(row=3,column=5,sticky=E)
		self.set_freq=Label(self.readout_frame,text="",font="Arial 10")
		self.set_freq.grid(row=5,column=5,sticky=E)
		self.set_lamu=Label(self.readout_frame,text="",font="Arial 10")
		self.set_lamu.grid(row=7,column=5,sticky=E)
		self.set_frequ=Label(self.readout_frame,text="",font="Arial 10")
		self.set_frequ.grid(row=9,column=5,sticky=E)

		self.lam=Label(self.readout_frame,text="",font="Arial 10")
		self.lam.grid(row=3,column=3,sticky=E)
		self.freq=Label(self.readout_frame,text="",font="Arial 10")
		self.freq.grid(row=5,column=3,sticky=E)
		self.lamu=Label(self.readout_frame,text="",font="Arial 10")
		self.lamu.grid(row=7,column=3,sticky=E)
		self.frequ=Label(self.readout_frame,text="",font="Arial 10")
		self.frequ.grid(row=9,column=3,sticky=E)
		self.pow=Label(self.readout_frame,text="",font="Arial 10")
		self.pow.grid(row=11,column=3,sticky=E)
		self.temp=Label(self.readout_frame,text="",font="Arial 10")
		self.temp.grid(row=13,column=3,sticky=E)


		#Buttons for setting wavelength/frequency and turning emission on/off.
		self.emission_on_button=Button(self.parent,text="Emission off",width=25,command=self.turn_on,font="Arial 10 bold",fg="red",relief=RAISED)
		self.emission_on_button.grid(row=3,column=3,padx=75,sticky=S,pady=40)

		self.set_button=Button(self.parent,text="Set wavelength",width=25,command=self.set_wvl,font="Arial 10 bold",relief=RAISED)
		self.set_button.grid(row=3,column=3,padx=75,sticky=N,pady=10)

		
		"""
		After all the parts of GUI are initialized, the program collects first information from the laser and
		updates GUI using obtained information.
		"""

		set_wavelength=self.laser.get_set_wavelength()
		set_frequency=self.laser.get_set_frequency()

		wavelength=self.laser.get_wavelength()
		frequency=self.laser.get_frequency()
		power=self.laser.get_power()
		temperature=self.laser.get_temperature()

		self.set_lam.configure(text="{0:.4f}".format(set_wavelength)+" nm")
		self.set_freq.configure(text="{0:.5f}".format(set_frequency)+" THz")
		self.set_lamu.configure(text="{0:.5f}".format(set_wavelength/4)+" nm")
		self.set_frequ.configure(text="{0:.5f}".format(set_frequency*4)+" THz")
		self.lam.configure(text="{0:.4f}".format(wavelength)+" nm")
		self.freq.configure(text="{0:.5f}".format(frequency)+" THz")
		self.lamu.configure(text="{0:.5f}".format(wavelength/4)+" nm")
		self.frequ.configure(text="{0:.5f}".format(frequency*4)+" THz")
		self.pow.configure(text="{0:.2f}".format(power)+" mW")
		self.temp.configure(text="{0:.2f}".format(temperature)+" C")


		"""
		After the first update, a few things are created: thread and queue (queues are thread-safe) for commands,
		Events that work like locks allowing threads to communicate with each other and stop each other. 
		"""

		self.is_updating=threading.Event()
		self.updating_paused=threading.Event()
		self.no_commands_left=threading.Event()
		self.command_queue=queue.Queue()
		self.command_thread=threading.Thread(target=self.execute_commands,kwargs={"com_queue":self.command_queue})
		self.command_thread.daemon=True #It stays open until the program is closed.

		"""
		After 200ms GUI will run it's updating function, where the updating thread is actually created and started. Here, also the command thread (listener) is started. 
		"""
		self.parent.after(200,self.update_params)
		self.command_thread.start()


		if config_wvl is not None:
			self.new_wv.set(str(config_wvl)) #Wavelength is set if non-default config file was provided.
			self.set_wvl()

	"""
	The following function runs in the command thread (it's the listener). It checks the queue for functions and
	their arguments and runs them to communicate with the device. The time.sleep function is added so that the
	laser can react to the command. 
	"""
	def execute_commands(self,com_queue):
		self.no_commands_left.set()
		while True:
			sleep(0.001)
			try:
				items=self.command_queue.get(False)

				self.is_updating.clear()

				func=items[0]
				if len(items)>1:
					args=items[1:]
				else:
					args=[]	
				self.updating_paused.wait()
				func(*args)	

				sleep(0.1)
				self.updating_paused.clear()
				self.is_updating.set()
			except queue.Empty:

				if not self.no_commands_left.is_set():
					self.no_commands_left.set()
				else:
					pass


	#Function that creates the update thread. After 300ms it runs the function that updates GUI.
	def update_params(self):

		self.thread_queue=queue.LifoQueue()
		self.update_thread=threading.Thread(target=self.update_queue,kwargs={'up_queue':self.thread_queue})
		self.update_thread.daemon=True
		self.update_thread.start()
		self.parent.after(300,self.update_labels)


	"""
	That's the function that's run in the update thread. It collcets data directly from the laser (every 300ms)
	and puts those obtained parameters into a queue.

	"""
	def update_queue(self,up_queue=None):

		self.is_updating.set()

		while True:

			sleep(0.2)

			if self.is_updating.is_set():

				try:
					wavelength=self.laser.get_wavelength()
					frequency=self.laser.get_frequency()
					power=self.laser.get_power()
					temperature=self.laser.get_temperature()
					err=self.laser.error_readout()
				except Exception as e:
					self.exception=e
				else:
					up_queue.put([wavelength,frequency,power,temperature,err])

			else:
				self.updating_paused.set()

	"""
	This function runs every 300ms in the GUI thread and checks the update queue for parameters taken from 
	the laser and puts them onto GUI. It also checks for errors from the device and puts them in a special
	window that's in a different pane. 
	"""
	def update_labels(self):

		try:
			res=self.thread_queue.get(0)

			if res[4]:
				raise DeviceError('Laser has encountered an error. Please reset.')
			elif self.exception is not None:
				raise self.exception

			wavelength=res[0]
			frequency=res[1]
			power=res[2]
			temperature=res[3]



			self.lam.configure(text="{0:.4f}".format(wavelength)+" nm")
			if power>1:
				self.status[2].configure(text="{0:.2f}".format(wavelength)+" nm")
			self.freq.configure(text="{0:.5f}".format(frequency)+" THz")
			self.lamu.configure(text="{0:.5f}".format(wavelength/4)+" nm")
			self.frequ.configure(text="{0:.5f}".format(frequency*4)+" THz")
			self.pow.configure(text="{0:.2f}".format(power)+" mW")
			self.temp.configure(text="{0:.2f}".format(temperature)+" C")

			self.parent.after(200,self.update_labels)

		except queue.Empty:

			self.parent.after(200,self.update_labels)

	"""
	Below are couple functions that are used to take information from the GUI and using that information,
	send appropriate commands to the laser through the command queue. In other words, these are the functions
	that put a laser-manipulation function and its arguments into the queue.
	"""
		
	def set_wvl(self):

		if self.new_wv.get()!="":
			try:
				wvl=float(self.new_wv.get())
			except ValueError:
				return	

			if wvl<self.min_wv:
				wvl=self.min_wv
				self.new_wv.set(wvl)
			elif wvl>self.max_wv:
				wvl=self.max_wv
				self.new_wv.set(wvl)

			self.set_lam.configure(text="{0:.4f}".format(wvl)+" nm")
			self.set_freq.configure(text="{0:.5f}".format(self.c/wvl)+" THz")
			self.set_lamu.configure(text="{0:.5f}".format(wvl/4)+" nm")
			self.set_frequ.configure(text="{0:.5f}".format(self.c/wvl*4)+" THz")

			self.no_commands_left.clear()

			self.command_queue.put([self.laser.set_wavelength,wvl])
		
		elif self.new_freq!="":
			try:
				freq=float(self.new_freq.get())
			except ValueError:
				return	

			if self.c/freq<self.min_wv:
				freq=self.c/self.min_wv
				self.new_freq.set(freq)
			elif self.c/freq>self.max_wv:
				freq=self.c/self.max_wv
				self.new_freq.set(freq)


			self.set_lam.configure(text="{0:.4f}".format(self.c/freq)+" nm")
			self.set_freq.configure(text="{0:.5f}".format(freq)+" THz")
			self.set_lamu.configure(text="{0:.5f}".format(self.c/freq/4)+" nm")
			self.set_frequ.configure(text="{0:.5f}".format(freq*4)+" THz")

			self.no_commands_left.clear()

			self.command_queue.put([self.laser.set_frequency,freq])


	def set_wvl_trace(self,*args):
		if self.new_wv.get()!="":
			self.new_freq_entry.config(state="disabled")
		elif self.new_freq.get()!="":
			self.new_wv_entry.config(state="disabled")
		else:
			self.new_wv_entry.config(state="normal")
			self.new_freq_entry.config(state="normal")


	def move_freq(self,val):

		freq=float(self.set_freq["text"][:-4])+val/1000 #It's faster to just read the GUI label than communicate with the laser.

		self.set_lam.configure(text="{0:.4f}".format(self.c/freq)+" nm")
		self.set_freq.configure(text="{0:.5f}".format(freq)+" THz")
		self.set_lamu.configure(text="{0:.5f}".format(self.c/freq/4)+" nm")
		self.set_frequ.configure(text="{0:.5f}".format(freq*4)+" THz")

		self.no_commands_left.clear()
		self.command_queue.put([self.laser.move_frequency,val])		
		
		
	def change_mod(self,*args):

		new_mod=self.mod_var.get()
		if new_mod=="Wide":
			self.command_queue.put([self.laser.modulation_type,0])
		elif new_mod=="Narrow":
			self.command_queue.put([self.laser.modulation_type,1])

	
	def turn_on(self,event=None):

		self.no_commands_left.clear()

		self.command_queue.put([self.laser.emission_on])
		
	
		cv=self.status[0]
		ov=self.status[1]
		wv=self.status[2]
		cv.itemconfig(ov,fill="#05FF2B")
		wvl=self.laser.get_wavelength()
		wv.configure(text="{0:.2f}".format(wvl)+" nm")
		self.emission_on_button.configure(text="Emission on",fg="green",command=self.turn_off,relief=SUNKEN)
		

	def turn_off(self,event=None):


		self.no_commands_left.clear()
		self.command_queue.put([self.laser.emission_off])
		# self.no_commands_left.wait()

		cv=self.status[0]
		ov=self.status[1]
		wv=self.status[2]
		cv.itemconfig(ov,fill="red")
		wv.configure(text="")
		self.emission_on_button.configure(text="Emission off",fg="red",command=self.turn_on,relief=RAISED)




#################################################################################################################

"""
The following class describes the pane that is used to control the transfer cavity. It controls scan parameters,
locking parameters, lockpoints of the master and slave lasers; allows sweeping the frequency within the ~GHz 
range; controlls the lasers and cavity through NI DAQs, and also controlls options associated with the DAQ.
"""

class TransferCavity:

	"""
	For the initialization we pass the parent frame (panes), the frame where plotting happens, list of laser 
	objects and the configuration file in the form of a dictionary.
	"""

	def __init__(self,parent,plt_frame,lasers,config,simulate):

		#Neighbouring plot frame
		self.plot_win=plt_frame

		#Initial configuration
		self.default_cfg=config

		#Main window
		self.parent=parent


		parent.grid_rowconfigure(0,minsize=2)
		parent.grid_rowconfigure(2,minsize=2)
		parent.grid_rowconfigure(4,minsize=2)
		parent.grid_rowconfigure(6,minsize=2)
		parent.grid_rowconfigure(8,minsize=2)

		parent.grid_rowconfigure(1,minsize=260,weight=1)
		parent.grid_rowconfigure(3,minsize=280,weight=2)
		parent.grid_rowconfigure(5,minsize=280,weight=2)
		parent.grid_rowconfigure(7,minsize=130,weight=1)

		parent.grid_columnconfigure(0,minsize=2)
		parent.grid_columnconfigure(2,minsize=2)

		parent.grid_columnconfigure(1,minsize=950,weight=1)


		self.lasers=lasers

		#Additional windows used for settings
		self.adset_window=None
		self.daqset_window=None


		"""
		Lock initialization. 
		This program can handle one or 2 lasers. The Lock class uses wavelength set on the 	laser as the
		argument for its initialization.
		"""
		if len(lasers)==1:
			self.lock=Lock([lasers[0].get_set_wavelength()],config)
			
		else:
			self.lock=Lock([lasers[0].get_set_wavelength(),lasers[1].get_set_wavelength()],config)
			# self.lock=Lock([1086,1087],config)


		"""
		Acquiring data and locking. 
		This class uses the previously defined lock to initialize. It also uses DAQ tasks that the program 
		initially crates using "setup_tasks" function. We also pass the config dictionary to this initialization 
		procedure.

		"""
		self.transfer_lock=TransferLock(self.lock,setup_tasks(config,len(lasers),simulate),config)

		
		"""
		Sweep thread.
		This part of the GUI operates mostly in its own thread. The exception is, however, the frequency sweeps,
		which might require waiting. To avoid freezing the GUI, sweep is done in a separate thread.
		"""
		self.sweep_thread=[threading.Thread(target=self.sweep_laser,kwargs={"ind":0}),threading.Thread(target=self.sweep_laser,kwargs={"ind":1})]

		self.cont_sweep_thread=[threading.Thread(target=self.cont_sweep_laser,kwargs={"ind":0}),threading.Thread(target=self.cont_sweep_laser,kwargs={"ind":1})]



		"""
		This part of the GUI is divided into 4 sections: the first one is frame where scanning optiones are
		located, the second and third ones are responsible for laser control, and finally the last one contains
		only some general-use buttons and possibly, in the future, wavelength measured directly from the wavemeter.
		"""

		#Scanning window. It is later subdivided into scan settings, lock settings and readout frames.
		self.cavity_window=LabelFrame(parent,text="Fabry-Perot Cavity")
		self.cavity_window.grid(row=1,column=1)

		self.cavity_window.grid_rowconfigure(0,minsize=5)
		self.cavity_window.grid_rowconfigure(2,minsize=5)
		self.cavity_window.grid_rowconfigure(4,minsize=10)
		self.cavity_window.grid_columnconfigure(0,minsize=10)
		self.cavity_window.grid_columnconfigure(2,minsize=10)
		self.cavity_window.grid_columnconfigure(4,minsize=10)
		self.cavity_window.grid_columnconfigure(6,minsize=10)



		#Scan settings subframe. 
		self.cavity_window_scan=LabelFrame(self.cavity_window,text="Scan")
		self.cavity_window_scan.grid(row=1,column=1,sticky=NW)

		self.cavity_window_scan.grid_rowconfigure(0,minsize=5)
		self.cavity_window_scan.grid_rowconfigure(2,minsize=10)
		self.cavity_window_scan.grid_rowconfigure(4,minsize=10)
		self.cavity_window_scan.grid_rowconfigure(6,minsize=10)
		self.cavity_window_scan.grid_rowconfigure(8,minsize=15)
		self.cavity_window_scan.grid_rowconfigure(10,minsize=15)
		self.cavity_window_scan.grid_columnconfigure(0,minsize=10)
		self.cavity_window_scan.grid_columnconfigure(2,minsize=10)
		self.cavity_window_scan.grid_columnconfigure(4,minsize=10)

		Label(self.cavity_window_scan,text="Scan offset [V]:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
		Label(self.cavity_window_scan,text="Scan amp. [V]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.cavity_window_scan,text="Scan time [ms]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.cavity_window_scan,text="Samples per scan:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		Label(self.cavity_window_scan,text="Move offset [mV]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)

		#These are containers for new values of different scanning settings.
		self.scan_off=StringVar()		#Scanning offset
		self.scan_t=StringVar()			#Scanning time
		self.samp_scan=StringVar()		#Samples per scan
		self.scan_amp=StringVar()		#Scan amplitude

		self.scan_off_entry=Entry(self.cavity_window_scan,textvariable=self.scan_off,width=12)
		self.scan_off_entry.grid(row=1,column=3)
		self.scan_t_entry=Entry(self.cavity_window_scan,textvariable=self.scan_t,width=12)
		self.scan_t_entry.grid(row=5,column=3)
		self.samp_scan_entry=Entry(self.cavity_window_scan,textvariable=self.samp_scan,width=12)
		self.samp_scan_entry.grid(row=7,column=3)
		self.scan_amp_entry=Entry(self.cavity_window_scan,textvariable=self.scan_amp,width=12)
		self.scan_amp_entry.grid(row=3,column=3)
		self.move_offset_m=Button(self.cavity_window_scan,width=4,text="-10",font="Arial 10 bold",command=lambda: self.move_scan_offset(-0.01))
		self.move_offset_m.grid(row=9,column=2,columnspan=2,sticky=W)
		self.move_offset_p=Button(self.cavity_window_scan,width=4,text="+10",font="Arial 10 bold",command=lambda: self.move_scan_offset(0.01))
		self.move_offset_p.grid(row=9,column=3,columnspan=2,padx=5,sticky=E)



		#Cavity lock settings subframe
		self.cavity_window_lock=LabelFrame(self.cavity_window,text="Lock")
		self.cavity_window_lock.grid(row=1,column=3,sticky=NW)

		self.cavity_window_lock.grid_rowconfigure(0,minsize=5)
		self.cavity_window_lock.grid_rowconfigure(2,minsize=5)
		self.cavity_window_lock.grid_rowconfigure(4,minsize=5)
		self.cavity_window_lock.grid_rowconfigure(6,minsize=5)
		self.cavity_window_lock.grid_rowconfigure(8,minsize=2)
		self.cavity_window_lock.grid_rowconfigure(10,minsize=2)
		self.cavity_window_lock.grid_rowconfigure(12,minsize=2)
		self.cavity_window_lock.grid_rowconfigure(14,minsize=5)

		self.cavity_window_lock.grid_columnconfigure(0,minsize=5)
		self.cavity_window_lock.grid_columnconfigure(2,minsize=13)
		self.cavity_window_lock.grid_columnconfigure(4,minsize=5)
		self.cavity_window_lock.grid_columnconfigure(6,minsize=7)

		Label(self.cavity_window_lock,text="Setpoint [ms]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.cavity_window_lock,text="P gain:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
		Label(self.cavity_window_lock,text="I gain:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.cavity_window_lock,text="Move lock [ms]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		
		#It as allowed to change lock setpoints and P and I gain. 
		self.lck_stp=StringVar()
		self.P_gain=StringVar()
		self.I_gain=StringVar()

		
		self.lck_stp_entry=Entry(self.cavity_window_lock,textvariable=self.lck_stp,width=12)
		self.lck_stp_entry.grid(row=5,column=3,columnspan=3)
		self.P_gain_entry=Entry(self.cavity_window_lock,textvariable=self.P_gain,width=12)
		self.P_gain_entry.grid(row=1,column=3,columnspan=3)
		self.I_gain_entry=Entry(self.cavity_window_lock,textvariable=self.I_gain,width=12)
		self.I_gain_entry.grid(row=3,column=3,columnspan=3)


		#To make manipulation easier, the lockpoint can also be moved in discrete steps. 
		self.plus1ms=Button(self.cavity_window_lock,text="+1",width=5,command=lambda: self.move_master_lck(1),font="Arial 10 bold")
		self.plus1ms.grid(row=7,column=5,sticky=E)
		self.plus5ms=Button(self.cavity_window_lock,text="+5",width=5,command=lambda: self.move_master_lck(5),font="Arial 10 bold")
		self.plus5ms.grid(row=9,column=5,sticky=E)
		self.plus10ms=Button(self.cavity_window_lock,text="+10",width=5,command=lambda: self.move_master_lck(10),font="Arial 10 bold")
		self.plus10ms.grid(row=11,column=5,sticky=E)
		self.minus1ms=Button(self.cavity_window_lock,text="-1",width=5,command=lambda: self.move_master_lck(-1),font="Arial 10 bold")
		self.minus1ms.grid(row=7,column=3,sticky=W)
		self.minus5ms=Button(self.cavity_window_lock,text="-5",width=5,command=lambda: self.move_master_lck(-5),font="Arial 10 bold")
		self.minus5ms.grid(row=9,column=3,sticky=W)
		self.minus10ms=Button(self.cavity_window_lock,text="-10",width=5,command=lambda: self.move_master_lck(-10),font="Arial 10 bold")
		self.minus10ms.grid(row=11,column=3,sticky=W)




		#Cavity information readout subframe
		self.cavity_window_readout=LabelFrame(self.cavity_window,text="Readout",height=200,width=350)
		self.cavity_window_readout.grid(row=1,column=5,sticky=NW)

		self.cavity_window_readout.grid_columnconfigure(0,minsize=5)
		self.cavity_window_readout.grid_columnconfigure(2,minsize=5)
		self.cavity_window_readout.grid_columnconfigure(3,minsize=40)
		self.cavity_window_readout.grid_columnconfigure(4,minsize=10)
		self.cavity_window_readout.grid_columnconfigure(5,minsize=30)
		self.cavity_window_readout.grid_columnconfigure(6,minsize=10)
		self.cavity_window_readout.grid_columnconfigure(7,minsize=50,weight=2)
		self.cavity_window_readout.grid_columnconfigure(8,minsize=10)
		self.cavity_window_readout.grid_columnconfigure(10,minsize=5)
		self.cavity_window_readout.grid_columnconfigure(12,minsize=10)

		self.cavity_window_readout.grid_rowconfigure(0,minsize=5)
		self.cavity_window_readout.grid_rowconfigure(2,minsize=5)
		self.cavity_window_readout.grid_rowconfigure(4,minsize=5)
		self.cavity_window_readout.grid_rowconfigure(6,minsize=5)
		self.cavity_window_readout.grid_rowconfigure(8,minsize=5)
		self.cavity_window_readout.grid_rowconfigure(10,minsize=11)
		self.cavity_window_readout.grid_rowconfigure(12,minsize=8)


		Label(self.cavity_window_readout,text="Scan offset [V]:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
		Label(self.cavity_window_readout,text="Scan frequency [Hz]:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		Label(self.cavity_window_readout,text="Scan step [mV]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.cavity_window_readout,text="Scan amplitude [V]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.cavity_window_readout,text="Samples per scan:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		Label(self.cavity_window_readout,text="P gain:",font="Arial 10 bold").grid(row=1,column=5,sticky=W)
		Label(self.cavity_window_readout,text="I gain:",font="Arial 10 bold").grid(row=3,column=5,sticky=W)
		Label(self.cavity_window_readout,text="Lock point [ms]:",font="Arial 10 bold").grid(row=5,column=5,sticky=W)
		Label(self.cavity_window_readout,text="Error rms [ms]:",font="Arial 10 bold").grid(row=7,column=5,sticky=W)
		Label(self.cavity_window_readout,text="Logging:",font="Arial 10 bold").grid(row=9,column=5,sticky=W)

		"""
		Many of the following variables are extracted from class containing scanning parameters. From here the
		path is follows: this class -> TransferLock -> DAQ_tasks -> Scan -> various attributes.
		"""

		self.real_scoff=Label(self.cavity_window_readout,text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.offset), font="Arial 10")
		self.real_scoff.grid(row=1,column=3,sticky=E)
		self.real_scfr=Label(self.cavity_window_readout,text='{:.1f}'.format(1000/self.transfer_lock.daq_tasks.ao_scan.scan_time), font="Arial 10")
		self.real_scfr.grid(row=7,column=3,sticky=E)
		self.real_scst=Label(self.cavity_window_readout,text='{:.1f}'.format(1000*self.transfer_lock.daq_tasks.ao_scan.scan_step), font="Arial 10")
		self.real_scst.grid(row=5,column=3,sticky=E)
		self.real_scamp=Label(self.cavity_window_readout,text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.amplitude), font="Arial 10")
		self.real_scamp.grid(row=3,column=3,sticky=E)
		self.real_samp=Label(self.cavity_window_readout,text='{:.0f}'.format(self.transfer_lock.daq_tasks.ao_scan.n_samples), font="Arial 10")
		self.real_samp.grid(row=9,column=3,sticky=E)
		self.real_pg=Label(self.cavity_window_readout,text='{:.3f}'.format(self.lock.prop_gain[0]), font="Arial 10")
		self.real_pg.grid(row=1,column=7,sticky=E)
		self.real_ig=Label(self.cavity_window_readout,text='{:.3f}'.format(self.lock.int_gain[0]), font="Arial 10")
		self.real_ig.grid(row=3,column=7,sticky=E)
		self.real_lckp=Label(self.cavity_window_readout,text='{:.0f}'.format(self.lock.master_lockpoint), font="Arial 10")
		self.real_lckp.grid(row=5,column=7,sticky=E)
		self.rms_cav=Label(self.cavity_window_readout,text="0", font="Arial 10")
		self.rms_cav.grid(row=7,column=7,sticky=E)

		
		#Checkbox indicating if the error signal from the cavity should be logged into a file.
		self.cav_err_log=IntVar()
		self.cav_err_log.set(0)
		self.cav_err_log_check=Checkbutton(self.cavity_window_readout,variable=self.cav_err_log)
		self.cav_err_log_check.grid(row=9,column=7,sticky=E)


		#Some visual indicators
		Label(self.cavity_window_readout,text="Lock:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)
		Label(self.cavity_window_readout,text="2 peaks:",font="Arial 10 bold").grid(row=11,column=9,sticky=W)
		Label(self.cavity_window_readout,text="Locked:",font="Arial 10 bold").grid(row=11,column=5,sticky=W)

		self.cav_lock_state=Label(self.cavity_window_readout,text="Disengaged", font="Arial 10 bold",fg="red")
		self.cav_lock_state.grid(row=11,column=1,sticky=E)

		self.twopeak_status_cv=Canvas(self.cavity_window_readout,height=20,width=20)
		self.twopeak_status_cv.grid(row=11,column=11,sticky=E)
		self.twopeak_status=self.twopeak_status_cv.create_oval(2,2,18,18,fill="red")

		self.cav_lock_status_cv=Canvas(self.cavity_window_readout,height=20,width=20)
		self.cav_lock_status_cv.grid(row=11,column=5,sticky=E)
		self.cav_lock_status=self.cav_lock_status_cv.create_oval(2,2,18,18,fill="red")


		#Button for additional settings in the cavity
		Label(self.cavity_window_readout,text="Additional",font="Arial 10 bold").grid(row=1,column=9,columnspan=3)
		Label(self.cavity_window_readout,text="settings:",font="Arial 10 bold").grid(row=2,column=9,columnspan=3,rowspan=2,sticky=N)

		butt_photo=PhotoImage(file="./SWP/images/sett1.png")
		self.cav_settings=Button(self.cavity_window_readout,image=butt_photo,command=self.open_cav_settings,width=50,height=50)
		self.cav_settings.image=butt_photo
		self.cav_settings.grid(row=5,column=9,columnspan=3,rowspan=5,sticky=N)


		#Additional buttons
		self.update_scan=Button(self.cavity_window,text="Update Scan",width=13, font="Arial 10 bold",command=self.update_scan_parameters)
		self.update_scan.grid(row=3,column=1,sticky=W)
		self.set_offset=Button(self.cavity_window,text="Set Offset",width=13, font="Arial 10 bold",command=self.set_scan_offset)
		self.set_offset.grid(row=3,column=1,sticky=E)
		self.update_lock=Button(self.cavity_window,text="Update Lock",width=13,command=self.update_master_lock,font="Arial 10 bold")
		self.update_lock.grid(row=3,column=3,sticky=W)
		self.engage_lock_button=Button(self.cavity_window,text="Engage Lock",width=13,command=self.engage_cavity_lock,font="Arial 10 bold")
		self.engage_lock_button.grid(row=3,column=3,sticky=E)



		"""
		Lasers' frame. This one is also divided into subframes. Two of those frames are initialized and created,
		although if there's only one laser connected, the bottom laser frame will be greyed out.
		"""

		self.laser_window=[LabelFrame(parent,text="Laser 1"),LabelFrame(parent,text="Laser 2")]
		self.laser_window[0].grid(row=3,column=1)
		self.laser_window[1].grid(row=5,column=1)


		self.laser_sweep=[]
		self.laser_lock=[]
		self.laser_readout=[]

		#Variables and labels for the laser frames. It's easier to define here and loop over number of lasers.
		self.stop_swp=False

		self.sweep_start=[StringVar(),StringVar()]
		self.sweep_stop=[StringVar(),StringVar()]
		self.sweep_step=[StringVar(),StringVar()]
		self.sweep_wait=[IntVar(),IntVar()]
		self.sweep_type=[StringVar(),StringVar()]
		self.sweep_speed=[IntVar(),IntVar()]
		self.sweep_start_entry=[None]*2
		self.sweep_stop_entry=[None]*2
		self.sweep_step_entry=[None]*2
		self.sweep_wait_entry=[None]*2
		self.sweep_type_entry=[None]*2
		self.sweep_speed_entry=[None]*2
		self.sweep_time_speed_label=[None]*2

		self.sw_progress=[None]*2
		self.sw_pr_var=[DoubleVar(),DoubleVar()]
		self.sw_button=[None]*2
		self.current_deviation=[None]*2
		self.current_dev_process=[None]*2
		
		self.set_volt=[None]*2
		self.new_volt_entry=[None]*2
		self.new_volt=[StringVar(),StringVar()]

		self.update_laser_lock_button=[None]*2
		self.engage_laser_lock_button=[None]*2

		self.laser_lock_state=[None]*2
		self.laser_lock_status_cv=[None]*2
		self.laser_lock_status=[None]*2

		self.laser_settings=[None]*2

		self.set_lfreq=[None]*2
		self.adj_fsr=[None]*2
		self.laser_r_lckp=[None]*2
		self.laser_r=[None]*2
		self.app_volt=[None]*2
		self.laser_pg=[None]*2
		self.laser_ig=[None]*2
		self.laser_lckp=[None]*2
		self.rms_laser=[None]*2

		self.laser_lsp_entry=[None]*2
		self.laser_P_entry=[None]*2
		self.laser_I_entry=[None]*2

		self.laser_lsp=[StringVar(),StringVar()]
		self.laser_P=[StringVar(),StringVar()]
		self.laser_I=[StringVar(),StringVar()]

		self.plus1MHz=[None]*2
		self.plus5MHz=[None]*2
		self.plus10MHz=[None]*2
		self.minus1MHz=[None]*2
		self.minus5MHz=[None]*2
		self.minus10MHz=[None]*2

		self.las_err_log=[IntVar(),IntVar()]
		self.las_err_log_check=[None]*2
		self.laser_logging_set=[False,False]
		self.master_logging_set=False
		self.log_las_file=[None]*2
		self.slave_err_log=[None]*2
		self.slave_time_log=[None]*2
		self.slave_rfreq_log=[None]*2
		self.slave_lfreq_log=[None]*2
		self.slave_rr_log=[None]*2
		self.slave_lr_log=[None]*2

		self.lt_start=[None]*2
		
		#We loop over two lasers. One of them might be just greyed out.
		for i in range(2):			

			self.laser_window[i].grid_rowconfigure(0,minsize=5)
			self.laser_window[i].grid_rowconfigure(2,minsize=5)
			self.laser_window[i].grid_rowconfigure(4,minsize=5)
			self.laser_window[i].grid_rowconfigure(6,minsize=10)
			self.laser_window[i].grid_columnconfigure(0,minsize=10)
			self.laser_window[i].grid_columnconfigure(2,minsize=10)
			self.laser_window[i].grid_columnconfigure(4,minsize=10)
			self.laser_window[i].grid_columnconfigure(6,minsize=10)


			#Laser sweep settings subframe
			self.laser_sweep.append(LabelFrame(self.laser_window[i],text="Frequency Sweep"))
			self.laser_sweep[-1].grid(row=1,column=1,sticky=NW)

			self.laser_sweep[-1].grid_rowconfigure(0,minsize=5)
			self.laser_sweep[-1].grid_rowconfigure(2,minsize=10)
			self.laser_sweep[-1].grid_rowconfigure(4,minsize=10)
			self.laser_sweep[-1].grid_rowconfigure(6,minsize=10)
			self.laser_sweep[-1].grid_rowconfigure(8,minsize=8)
			self.laser_sweep[-1].grid_rowconfigure(10,minsize=10)
			self.laser_sweep[-1].grid_columnconfigure(0,minsize=10)
			self.laser_sweep[-1].grid_columnconfigure(2,minsize=10)
			self.laser_sweep[-1].grid_columnconfigure(4,minsize=10)

			Label(self.laser_sweep[-1],text="Sweep start [MHz]:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
			Label(self.laser_sweep[-1],text="Sweep stop [MHz]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
			Label(self.laser_sweep[-1],text="Sweep step [MHz]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
			self.sweep_time_speed_label[i]=Label(self.laser_sweep[-1],text="Wait time [s]:",font="Arial 10 bold")
			self.sweep_time_speed_label[i].grid(row=7,column=1,sticky=W)
			Label(self.laser_sweep[-1],text="Sweep type:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)

			self.sweep_start_entry[i]=Entry(self.laser_sweep[-1],textvariable=self.sweep_start[i],width=12)
			self.sweep_start_entry[i].grid(row=1,column=3)
			self.sweep_stop_entry[i]=Entry(self.laser_sweep[-1],textvariable=self.sweep_stop[i],width=12)
			self.sweep_stop_entry[i].grid(row=3,column=3)
			self.sweep_step_entry[i]=Entry(self.laser_sweep[-1],textvariable=self.sweep_step[i],width=12)
			self.sweep_step_entry[i].grid(row=5,column=3)
			self.sweep_wait_entry[i]=OptionMenu(self.laser_sweep[-1],self.sweep_wait[i],3,4,5,6,7,8,9,10,12,14,16,18,20,25,30,40,50,60)
			self.sweep_wait_entry[i].grid(row=7,column=3)
			self.sweep_wait[i].set(3)
			self.sweep_type_entry[i]=OptionMenu(self.laser_sweep[-1],self.sweep_type[i],"Discrete","Cont.")
			self.sweep_type_entry[i].grid(row=9,column=3)
			self.sweep_type_entry[i].config(width=7)
			self.sweep_type[i].set("Discrete")
			self.sweep_type[i].trace('w',lambda n1,n2,op,x=i:self.sweep_type_change(x,n1,n2,op))


			#Laser lock settings subframe
			self.laser_lock.append(LabelFrame(self.laser_window[i],text="Lock"))
			self.laser_lock[-1].grid(row=1,column=3,sticky=NW)

			self.laser_lock[-1].grid_rowconfigure(0,minsize=5)
			self.laser_lock[-1].grid_rowconfigure(2,minsize=5)
			self.laser_lock[-1].grid_rowconfigure(4,minsize=5)
			self.laser_lock[-1].grid_rowconfigure(6,minsize=5)
			self.laser_lock[-1].grid_rowconfigure(8,minsize=2)
			self.laser_lock[-1].grid_rowconfigure(10,minsize=2)
			self.laser_lock[-1].grid_rowconfigure(12,minsize=2)
			self.laser_lock[-1].grid_rowconfigure(14,minsize=5)

			self.laser_lock[-1].grid_columnconfigure(0,minsize=5)
			self.laser_lock[-1].grid_columnconfigure(2,minsize=5)
			self.laser_lock[-1].grid_columnconfigure(4,minsize=5)
			self.laser_lock[-1].grid_columnconfigure(6,minsize=7)

			Label(self.laser_lock[-1],text="Setpoint [MHz]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
			Label(self.laser_lock[-1],text="P gain:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
			Label(self.laser_lock[-1],text="I gain:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
			Label(self.laser_lock[-1],text="Move lock [MHz]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
			

			
			self.laser_lsp_entry[i]=Entry(self.laser_lock[-1],textvariable=self.laser_lsp[i],width=12)
			self.laser_lsp_entry[i].grid(row=5,column=3,columnspan=3)
			self.laser_P_entry[i]=Entry(self.laser_lock[-1],textvariable=self.laser_P[i],width=12)
			self.laser_P_entry[i].grid(row=1,column=3,columnspan=3)
			self.laser_I_entry[i]=Entry(self.laser_lock[-1],textvariable=self.laser_I[i],width=12)
			self.laser_I_entry[i].grid(row=3,column=3,columnspan=3)

			"""
			Buttons below are defined to move the lock in discrete steps. One can pass arguments to commands 
			attached to Button widget by using "lambda" command in Python.
			"""
			self.plus1MHz[i]=Button(self.laser_lock[-1],text="+1",width=5,command=lambda x=i: self.move_slave_lck(1,x),font="Arial 10 bold")
			self.plus1MHz[i].grid(row=7,column=5,sticky=E)
			self.plus5MHz[i]=Button(self.laser_lock[-1],text="+5",width=5,command=lambda x=i: self.move_slave_lck(5,x),font="Arial 10 bold")
			self.plus5MHz[i].grid(row=9,column=5,sticky=E)
			self.plus10MHz[i]=Button(self.laser_lock[-1],text="+10",width=5,command=lambda x=i: self.move_slave_lck(10,x),font="Arial 10 bold")
			self.plus10MHz[i].grid(row=11,column=5,sticky=E)
			self.minus1MHz[i]=Button(self.laser_lock[-1],text="-1",width=5,command=lambda x=i: self.move_slave_lck(-1,x),font="Arial 10 bold")
			self.minus1MHz[i].grid(row=7,column=3,sticky=W)
			self.minus5MHz[i]=Button(self.laser_lock[-1],text="-5",width=5,command=lambda x=i: self.move_slave_lck(-5,x),font="Arial 10 bold")
			self.minus5MHz[i].grid(row=9,column=3,sticky=W)
			self.minus10MHz[i]=Button(self.laser_lock[-1],text="-10",width=5,command=lambda x=i: self.move_slave_lck(-10,x),font="Arial 10 bold")
			self.minus10MHz[i].grid(row=11,column=3,sticky=W)



			#Laser information readout subframe
			self.laser_readout.append(LabelFrame(self.laser_window[i],text="Readout"))
			self.laser_readout[-1].grid(row=1,column=5,sticky=NW)

			self.laser_readout[-1].grid_columnconfigure(0,minsize=5)
			self.laser_readout[-1].grid_columnconfigure(2,minsize=5)
			self.laser_readout[-1].grid_columnconfigure(3,minsize=30)
			self.laser_readout[-1].grid_columnconfigure(4,minsize=10)
			self.laser_readout[-1].grid_columnconfigure(5,minsize=30)
			self.laser_readout[-1].grid_columnconfigure(6,minsize=10)
			self.laser_readout[-1].grid_columnconfigure(7,minsize=50,weight=2)
			self.laser_readout[-1].grid_columnconfigure(8,minsize=10)
			self.laser_readout[-1].grid_columnconfigure(10,minsize=5)
			self.laser_readout[-1].grid_columnconfigure(12,minsize=5)

			self.laser_readout[-1].grid_rowconfigure(0,minsize=5)
			self.laser_readout[-1].grid_rowconfigure(2,minsize=5)
			self.laser_readout[-1].grid_rowconfigure(4,minsize=5)
			self.laser_readout[-1].grid_rowconfigure(6,minsize=5)
			self.laser_readout[-1].grid_rowconfigure(8,minsize=5)
			self.laser_readout[-1].grid_rowconfigure(10,minsize=11)
			self.laser_readout[-1].grid_rowconfigure(12,minsize=8)
			

			Label(self.laser_readout[-1],text="Set freq. [THz]:",font="Arial 10 bold").grid(row=1,column=1,sticky=W)
			Label(self.laser_readout[-1],text="Adjusted FSR [MHz]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
			Label(self.laser_readout[-1],text="Lock point [R]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
			Label(self.laser_readout[-1],text="Current R:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
			Label(self.laser_readout[-1],text="App. voltage [V]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
			Label(self.laser_readout[-1],text="P gain:",font="Arial 10 bold").grid(row=1,column=5,sticky=W)
			Label(self.laser_readout[-1],text="I gain:",font="Arial 10 bold").grid(row=3,column=5,sticky=W)
			Label(self.laser_readout[-1],text="Lock point [MHz]:",font="Arial 10 bold").grid(row=5,column=5,sticky=W)
			Label(self.laser_readout[-1],text="Error rms [MHz]:",font="Arial 10 bold").grid(row=7,column=5,sticky=W)
			Label(self.laser_readout[-1],text="Logging:",font="Arial 10 bold").grid(row=9,column=5,sticky=W)

			#The first option is realized if there is only one laser - the second laser frame has no data.
			if len(lasers)==1 and i==1: 
				self.set_lfreq[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.set_lfreq[i].grid(row=1,column=3,sticky=E)
				self.adj_fsr[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.adj_fsr[i].grid(row=3,column=3,sticky=E)
				self.laser_r_lckp[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.laser_r_lckp[i].grid(row=5,column=3,sticky=E)
				self.laser_r[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.laser_r[i].grid(row=7,column=3,sticky=E)
				self.app_volt[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.app_volt[i].grid(row=9,column=3,sticky=E)
				self.laser_pg[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.laser_pg[i].grid(row=1,column=7,sticky=E)
				self.laser_ig[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.laser_ig[i].grid(row=3,column=7,sticky=E)

				self.laser_lckp[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.laser_lckp[i].grid(row=5,column=7,sticky=E)
				self.rms_laser[i]=Label(self.laser_readout[-1],text='', font="Arial 10")
				self.rms_laser[i].grid(row=7,column=7,sticky=E)

				self.laser_lock_status_cv[i]=Canvas(self.laser_readout[-1],height=20,width=20)
				self.laser_lock_status_cv[i].grid(row=11,column=5,sticky=E)
				self.laser_lock_status[i]=self.laser_lock_status_cv[i].create_oval(2,2,18,18,fill="#F0F0ED")

			else:

				self.set_lfreq[i]=Label(self.laser_readout[-1],text='{:.2f}'.format(self.lock.slave_freqs[i]/1000), font="Arial 10")
				self.set_lfreq[i].grid(row=1,column=3,sticky=E)
				self.adj_fsr[i]=Label(self.laser_readout[-1],text='{:.1f}'.format(1000*self.lock._slave_FSR[i]), font="Arial 10")
				self.adj_fsr[i].grid(row=3,column=3,sticky=E)
				self.laser_r_lckp[i]=Label(self.laser_readout[-1],text='{:.3f}'.format(self.lock.slave_lockpoints[i]), font="Arial 10")
				self.laser_r_lckp[i].grid(row=5,column=3,sticky=E)
				self.laser_r[i]=Label(self.laser_readout[-1],text='{:.3f}'.format(self.lock.slave_Rs[i]), font="Arial 10")
				self.laser_r[i].grid(row=7,column=3,sticky=E)
				self.app_volt[i]=Label(self.laser_readout[-1],text='{:.3f}'.format(self.transfer_lock.daq_tasks.ao_laser.voltages[i]), font="Arial 10")
				self.app_volt[i].grid(row=9,column=3,sticky=E)
				self.laser_pg[i]=Label(self.laser_readout[-1],text='{:.3f}'.format(self.lock.prop_gain[i+1]), font="Arial 10")
				self.laser_pg[i].grid(row=1,column=7,sticky=E)
				self.laser_ig[i]=Label(self.laser_readout[-1],text='{:.3f}'.format(self.lock.int_gain[i+1]), font="Arial 10")
				self.laser_ig[i].grid(row=3,column=7,sticky=E)

				self.laser_lckp[i]=Label(self.laser_readout[-1],text='{:.0f}'.format(self.lock.get_laser_lockpoint(i)), font="Arial 10")
				self.laser_lckp[i].grid(row=5,column=7,sticky=E)
				self.rms_laser[i]=Label(self.laser_readout[-1],text="0", font="Arial 10")
				self.rms_laser[i].grid(row=7,column=7,sticky=E)

				self.laser_lock_status_cv[i]=Canvas(self.laser_readout[-1],height=20,width=20)
				self.laser_lock_status_cv[i].grid(row=11,column=5,sticky=E)
				self.laser_lock_status[i]=self.laser_lock_status_cv[i].create_oval(2,2,18,18,fill="red")

			
			#Checkbutton for logging error signal to file. 
			self.las_err_log[i].set(0)
			self.las_err_log_check[i]=Checkbutton(self.laser_readout[-1],variable=self.las_err_log[i])
			self.las_err_log_check[i].grid(row=9,column=7,sticky=E)


			#Visual indicators. 
			Label(self.laser_readout[-1],text="Lock:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)
			Label(self.laser_readout[-1],text="Locked:",font="Arial 10 bold").grid(row=11,column=5,sticky=W)

			self.laser_lock_state[i]=Label(self.laser_readout[-1],text="Disengaged", font="Arial 10 bold",fg="red")
			self.laser_lock_state[i].grid(row=11,column=1,sticky=E)


			#Button for additional settings window
			Label(self.laser_readout[-1],text="Additional",font="Arial 10 bold").grid(row=1,column=9,columnspan=3)
			Label(self.laser_readout[-1],text="settings:",font="Arial 10 bold").grid(row=2,column=9,columnspan=3,rowspan=2,sticky=N)

			butt_photo=PhotoImage(file="./SWP/images/las_set.png")
			self.laser_settings[i]=Button(self.laser_readout[-1],image=butt_photo,command=lambda x=i: self.open_las_settings(x),width=50,height=50)
			self.laser_settings[i].image=butt_photo
			self.laser_settings[i].grid(row=5,column=9,columnspan=3,rowspan=5,sticky=N)



			#Additional buttons
			self.update_laser_lock_button[i]=Button(self.laser_window[i],text="Update Lock",width=13,command=lambda x=i: self.update_laser_lock(x),font="Arial 10 bold")
			self.update_laser_lock_button[i].grid(row=3,column=3,sticky=W)
			self.engage_laser_lock_button[i]=Button(self.laser_window[i],text="Engage Lock",width=13,command=lambda x=i: self.engage_laser_lock(x),font="Arial 10 bold")
			self.engage_laser_lock_button[i].grid(row=3,column=3,sticky=E)
			self.sw_button[i]=Button(self.laser_window[i],text="Sweep",width=28,command=lambda x=i: self.sweep_laser_th(x),font="Arial 10 bold")
			self.sw_button[i].grid(row=3,column=1)
			self.set_volt[i]=Button(self.laser_window[i],width=12,text="Set Voltage",font="Arial 10 bold",command=lambda x=i:self.set_voltage(x))
			self.set_volt[i].grid(row=3,column=5,sticky=W,padx=250)


			#Progress bar and labels for the frequency sweep.
			self.sw_progress[i]=ttk.Progressbar(self.laser_window[i],orient=HORIZONTAL,length=700,maximum=100,mode='determinate',variable=self.sw_pr_var[i])
			self.sw_progress[i].grid(row=5,column=1,columnspan=5,sticky=W)

			self.current_deviation[i]=Label(self.laser_window[i],text="",font="Arial 10")
			self.current_deviation[i].grid(row=5,column=5,sticky=W,padx=210)

			self.current_dev_process[i]=Label(self.laser_window[i],text="",font="Arial 10")
			self.current_dev_process[i].grid(row=5,column=5,sticky=W,padx=330)


			#Additional option for directly changing voltage applied to the laser. 
			Label(self.laser_window[i],text="New voltage [V]:",font="Arial 10 bold").grid(row=3,column=5,sticky=W,padx=9)

			self.new_volt_entry[i]=Entry(self.laser_window[i],width=12,textvariable=self.new_volt[i])
			self.new_volt_entry[i].grid(row=3,column=5,sticky=W,padx=130)
			
			


		#Bottom frame 
		self.bottom_frame=Frame(parent,width=950)
		self.bottom_frame.grid(row=7,column=1,sticky=W)

		self.bottom_frame.grid_columnconfigure(0,minsize=10)
		self.bottom_frame.grid_columnconfigure(2,minsize=10)
		self.bottom_frame.grid_columnconfigure(4,minsize=10)
		self.bottom_frame.grid_columnconfigure(6,minsize=10)
		self.bottom_frame.grid_rowconfigure(0,minsize=5)
		self.bottom_frame.grid_rowconfigure(2,minsize=10)
		self.bottom_frame.grid_rowconfigure(4,minsize=10)
		self.bottom_frame.grid_rowconfigure(8,minsize=5)


		#Buttons for: starting the scan, changing DAQ channels and saving current configuration to a config file.
		self.run_scan=Button(self.bottom_frame,text="Start Scanning",width=20,command=self.start_scanning,font="Arial 12 bold")
		self.run_scan.grid(row=1,column=1,sticky=W)

		self.save_configuration=Button(self.bottom_frame,text="Save Settings",width=20,command=self.save_config,font="Arial 12 bold")
		self.save_configuration.grid(row=3, column=1,sticky=W)

		self.change_channels=Button(self.bottom_frame,text="Change DAQ channels",width=20,command=self.change_daq_channels,font="Arial 12 bold")
		self.change_channels.grid(row=5, column=1,sticky=W)



		#Finally, if there's only one laser, the second laser frame is greyed out.
		if len(lasers)==1:
			for child in self.laser_window[1].winfo_children():
				try:
					child.config(state="disabled")
				except TclError:
					for grandchild in child.winfo_children():
						try:
							grandchild.config(state="disabled")
						except TclError:
							pass

		#Additional flag showing whether or not the scan is currently running.



	"""
	Next, there are several methods that are used by this class. Appart from the one that controls the frequency 
	sweep, they operate in the same thread as the GUI. The first method is command invoke by clicking "Save Config"
	button. This method first opens the filedialog window askign to choose a file to save to. The window is opened
	in the "/config" directory inside the directory of the GUI file (so the directory of app's initialization).
	Then, a dictionary is created, if a file was chosen, with information being saved. The information that is
	included (in order):
	- DAQ:
		*name of the device
	- CAVITY:
		*how many points are collected to calculate RMS's 
		*what the RMS is for the cavity below which it is considered locked
		*criterion for peak finding 
		*time of the scan (ms)
		*number of samples per scan
		*offset of the scan (V)
		*amplitude of the scan (V)
		*proportional gain
		*integral gain
		*FSR (GHz)
		*wavelength of the master laser (nm)
		*master laser's/cavity's lockpoint (ms)
		*minimum voltage that can applied to cavity's piezo (V)
		*maximum voltage (V)
		*input channel number
		*output channel number
	-LASER:
		*lockpoint in units of R parameter
		*lockpoint in MHz units, where 0 MHz corresponds to R=0.5
		*wavelength to which the NKT laser is set (nm)
		*criterion for peak finding 
		*what the RMS is for the laser below which it is considered locked
		*proportional gain
		*integral gain
		*minimum voltage that can applied to laser's piezo (V)
		*maximum voltage (V)
		*voltage applied to laser's piezo (V)
		*input channel number
		*output channel number

	Once the dictionaries are created, they are passed to a function (in file "Config.py") that saves them to an
	.ini file.
	"""
	def save_config(self):
		flname=filedialog.asksaveasfilename(initialdir = os.path.dirname(os.path.realpath(__file__))+"/configs",title = "Select file",filetypes = (("config files","*.ini"),))

		if flname=="":
			return

		daq_d={"DeviceName":self.transfer_lock.daq_tasks.device.name}

		cav_d={"RMS":self.transfer_lock.rms_points,"LockThreshold":self.transfer_lock.master_rms_crit,"PeakCriterion":self.transfer_lock.master_peak_crit,"ScanTime":self.transfer_lock.daq_tasks.ao_scan.scan_time,"ScanSamples":self.transfer_lock.daq_tasks.ao_scan.n_samples,"ScanOffset":self.transfer_lock.daq_tasks.ao_scan.offset,"ScanAmplitude":self.transfer_lock.daq_tasks.ao_scan.amplitude,"PGain":self.lock.prop_gain[0],"IGain":self.lock.int_gain[0],"FSR":self.lock._FSR,"Wavelength":self.lock.get_master_wavelength(),"Lockpoint":self.lock.master_lockpoint,"MinVoltage":self.transfer_lock.daq_tasks.ao_scan.mn_voltage,"MaxVoltage":self.transfer_lock.daq_tasks.ao_scan.mx_voltage,"InputChannel":channel_number(self.transfer_lock.daq_tasks.get_scan_ai_channel()),"OutputChannel":channel_number(self.transfer_lock.daq_tasks.get_scan_ao_channel())}
		
		laser1_d={"LockpointR":self.lock.slave_lockpoints[0],"LockpointMHz":self.lock.get_laser_lockpoint(0),"Wavelength":self.lasers[0].get_set_wavelength(),"PeakCriterion":self.transfer_lock.slave_peak_crits[0],"LockThreshold":self.transfer_lock.slave_rms_crits[0],"PGain":self.lock.prop_gain[1],"IGain":self.lock.int_gain[1],"MinVoltage":self.transfer_lock.daq_tasks.ao_laser.mn_voltages[0],"MaxVoltage":self.transfer_lock.daq_tasks.ao_laser.mx_voltages[0],"SetVoltage":self.transfer_lock.daq_tasks.ao_laser.voltages[0],"InputChannel":channel_number(self.transfer_lock.daq_tasks.get_laser_ai_channel(0)),"OutputChannel":channel_number(self.transfer_lock.daq_tasks.get_laser_ao_channel(0))}
		
		if len(self.lasers)>1:
		
			laser2_d={"LockpointR":self.lock.slave_lockpoints[1],"LockpointMHz":self.lock.get_laser_lockpoint(1),"Wavelength":self.lasers[1].get_set_wavelength(),"PeakCriterion":self.transfer_lock.slave_peak_crits[1],"LockThreshold":self.transfer_lock.slave_rms_crits[1],"PGain":self.lock.prop_gain[2],"IGain":self.lock.int_gain[2],"MinVoltage":self.transfer_lock.daq_tasks.ao_laser.mn_voltages[1],"MaxVoltage":self.transfer_lock.daq_tasks.ao_laser.mx_voltages[1],"SetVoltage":self.transfer_lock.daq_tasks.ao_laser.voltages[1],"InputChannel":channel_number(self.transfer_lock.daq_tasks.get_laser_ai_channel(1)),"OutputChannel":channel_number(self.transfer_lock.daq_tasks.get_laser_ao_channel(1))}
		
			save_conf(flname,daq_d,cav_d,laser1_d,laser2_d)
		
		else:
			save_conf(flname,daq_d,cav_d,laser1_d)


	#Simple function changing GUI element when option is changed.
	def sweep_type_change(self,ind,*args):

		typ=self.sweep_type[ind].get()
		if typ=="Cont.":
			self.sweep_time_speed_label[ind].config(text="Speed [MHz/s]:")
			self.sweep_wait_entry[ind].destroy()
			self.sweep_speed_entry[ind]=OptionMenu(self.laser_sweep[ind],self.sweep_speed[ind],1,2,3,4,5,6,7,8,9,10)
			self.sweep_speed_entry[ind].grid(row=7,column=3)
			self.sweep_speed[ind].set(5)
			self.sweep_step_entry[ind].config(state="disabled")
			self.sw_button[ind].configure(command=lambda x=ind: self.conitnuous_sweep_th(x))

		elif typ=="Discrete":
			self.sweep_time_speed_label[ind].config(text="Wait time [s]:")
			self.sweep_speed_entry[ind].destroy()
			self.sweep_wait_entry[ind]=OptionMenu(self.laser_sweep[ind],self.sweep_wait[ind],3,4,5,6,7,8,9,10,12,14,16,18,20,25,30,40,50,60)
			self.sweep_wait_entry[ind].grid(row=7,column=3)
			self.sweep_wait[ind].set(3)
			self.sweep_step_entry[ind].config(state="normal")
			self.sw_button[ind].configure(command=lambda x=ind: self.sweep_laser_th(x))

	"""
	The function below opens a separate small window that shows currently used DAQ channels: outputs for cavity 
	scanning and lasers' piezo control, and inputs taking information from photodetectors for master and slave
	lasers. The window allows to change these channels.
	"""
	def change_daq_channels(self):


		n=len(self.lasers)

		#We open a different window on top of the window cotaining the main GUI and move focus towards the new window.
		self.daqset_window=Toplevel(self.parent,height=600,width=700)
		self.daqset_window.title("DAQ channels settings")
		self.daqset_window.bind("<Escape>",self.cancel_daqtop)

		self.daqset_window.grid_columnconfigure(0,minsize=20)
		self.daqset_window.grid_columnconfigure(2,minsize=30)
		self.daqset_window.grid_columnconfigure(4,minsize=30)
		self.daqset_window.grid_columnconfigure(6,minsize=30)

		self.daqset_window.grid_rowconfigure(0,minsize=10)
		self.daqset_window.grid_rowconfigure(2,minsize=10)
		self.daqset_window.grid_rowconfigure(4,minsize=10)
		self.daqset_window.grid_rowconfigure(6,minsize=10)
		self.daqset_window.grid_rowconfigure(8,minsize=10)
		self.daqset_window.grid_rowconfigure(10,minsize=10)
		self.daqset_window.grid_rowconfigure(12,minsize=10)
		self.daqset_window.grid_rowconfigure(14,minsize=30)
		self.daqset_window.grid_rowconfigure(16,minsize=10)

		Label(self.daqset_window,text="Current Settings",font="Arial 10 bold").grid(row=1,column=5)

		Label(self.daqset_window,text="Scan output channel:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.daqset_window,text="Laser 1 output channel:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		if n>1:
			Label(self.daqset_window,text="Laser 2 output channel:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		else:
			Label(self.daqset_window,text="Laser 2 output channel:",font="Arial 10 bold",state="disabled").grid(row=7,column=1,sticky=W)
		Label(self.daqset_window,text="Master laser input channel:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		Label(self.daqset_window,text="Laser 1 input channel:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)
		if n>1:
			Label(self.daqset_window,text="Laser 2 input channel:",font="Arial 10 bold").grid(row=13,column=1,sticky=W)
		else:
			Label(self.daqset_window,text="Laser2 input channel:",font="Arial 10 bold",state="disabled").grid(row=13,column=1,sticky=W)


		#We obtain all the channel names of the channels that are currently in use. 
		self.new_scan_ao=StringVar()
		self.new_scan_ao.set(self.transfer_lock.daq_tasks.get_scan_ao_channel())
		self.new_las1_ao=StringVar()
		self.new_las1_ao.set(self.transfer_lock.daq_tasks.get_laser_ao_channel(0))
		self.new_las1_ai=StringVar()
		self.new_las1_ai.set(self.transfer_lock.daq_tasks.get_laser_ai_channel(0))
		self.new_master_ai=StringVar()
		self.new_master_ai.set(self.transfer_lock.daq_tasks.get_scan_ai_channel())
		if n>1:
			self.new_las2_ao=StringVar()
			self.new_las2_ao.set(self.transfer_lock.daq_tasks.get_laser_ao_channel(1))
			self.new_las2_ai=StringVar()
			self.new_las2_ai.set(self.transfer_lock.daq_tasks.get_laser_ai_channel(1))


		AO_channels=self.transfer_lock.daq_tasks.get_ao_channel_names() #All analog output channels on the device
		AI_channels=self.transfer_lock.daq_tasks.get_ai_channel_names() #All analog input channels on the device

		#Cavity, lasers and photodetectors can have their channel changed using following option menus. 
		self.new_scan_ao_entry=OptionMenu(self.daqset_window,self.new_scan_ao,*AO_channels)
		self.new_scan_ao_entry.grid(row=3,column=3)
		self.new_las1_ao_entry=OptionMenu(self.daqset_window,self.new_las1_ao,*AO_channels)
		self.new_las1_ao_entry.grid(row=5,column=3)
		if n>1:
			self.new_las2_ao_entry=OptionMenu(self.daqset_window,self.new_las2_ao,*AO_channels)
			self.new_las2_ao_entry.grid(row=7,column=3)
		self.new_master_ai_entry=OptionMenu(self.daqset_window,self.new_master_ai,*AI_channels)
		self.new_master_ai_entry.grid(row=9,column=3)
		self.new_las1_ai_entry=OptionMenu(self.daqset_window,self.new_las1_ai,*AI_channels)
		self.new_las1_ai_entry.grid(row=11,column=3)
		if n>1:
			self.new_las2_ai_entry=OptionMenu(self.daqset_window,self.new_las2_ai,*AI_channels)
			self.new_las2_ai_entry.grid(row=13,column=3)
		

		Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_scan_ao_channel(),font="Arial 10").grid(row=3,column=5,sticky=E)
		Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_laser_ao_channel(0),font="Arial 10").grid(row=5,column=5,sticky=E)
		if n>1:
			Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_laser_ao_channel(1),font="Arial 10").grid(row=7,column=5,sticky=E)
		else:
			Label(self.daqset_window,text="None",font="Arial 10",state="disabled").grid(row=7,column=5,sticky=E)
		Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_scan_ai_channel(),font="Arial 10").grid(row=9,column=5,sticky=E)
		Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_laser_ai_channel(0),font="Arial 10").grid(row=11,column=5,sticky=E)
		if n>1:
			Label(self.daqset_window,text=self.transfer_lock.daq_tasks.get_laser_ai_channel(1),font="Arial 10").grid(row=13,column=5,sticky=E)
		else:
			Label(self.daqset_window,text="None",font="Arial 10",state="disabled").grid(row=13,column=5,sticky=E)

		#Buttons in this window are packed in a separate frame at the bottom. Buttons are: Update, Cancel, Reset
		self.button_frame_ad=Frame(self.daqset_window)
		self.button_frame_ad.grid(row=15,column=1,columnspan=5)

		self.button_frame_ad.grid_columnconfigure(1,minsize=10)
		self.button_frame_ad.grid_columnconfigure(3,minsize=10)

		Button(self.button_frame_ad,command=self.update_daq_channels,text="Update Channels",font="Arial 10 bold",width=15).grid(row=0,column=0,sticky=W)
		Button(self.button_frame_ad,command=self.cancel_daqtop,text="Cancel",font="Arial 10 bold",width=15).grid(row=0,column=2)
		Button(self.button_frame_ad,command=self.reset_tasks,text="Reset Channels",font="Arial 10 bold",width=15).grid(row=0,column=4,sticky=E)

		self.daqset_window.focus_force() #Focusing on the window when it opens


	"""
	Next method opens a separate window that allows to change some settings regarding the cavity, the scan and the 
	master laser, as well as some miscellaneous criteria.
	"""
	def open_cav_settings(self):

		#Additional settnigs window

		self.adset_window=Toplevel(self.parent,height=600,width=600)
		self.adset_window.title("Additional scan settings")
		self.adset_window.bind("<Escape>",self.cancel_top)


		self.adset_window.grid_columnconfigure(0,minsize=20)
		self.adset_window.grid_columnconfigure(2,minsize=30)
		self.adset_window.grid_columnconfigure(4,minsize=20)
		self.adset_window.grid_columnconfigure(6,minsize=20)

		self.adset_window.grid_rowconfigure(0,minsize=10)
		self.adset_window.grid_rowconfigure(2,minsize=5)
		self.adset_window.grid_rowconfigure(4,minsize=5)
		self.adset_window.grid_rowconfigure(6,minsize=5)
		self.adset_window.grid_rowconfigure(8,minsize=5)
		self.adset_window.grid_rowconfigure(10,minsize=5)
		self.adset_window.grid_rowconfigure(12,minsize=5)
		self.adset_window.grid_rowconfigure(14,minsize=5)
		self.adset_window.grid_rowconfigure(16,minsize=30)
		self.adset_window.grid_rowconfigure(18,minsize=10)

		#Labels 
		Label(self.adset_window,text="Current Settings",font="Arial 10 bold").grid(row=1,column=5)
		Label(self.adset_window,text="Cavity FSR [MHz]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.adset_window,text="Cavity min. voltage [V]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.adset_window,text="Cavity max. voltage [V]:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		Label(self.adset_window,text="Master Laser Wavelength [nm]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		Label(self.adset_window,text="RMS Master Lock Threshold [ms]:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)
		Label(self.adset_window,text="RMS Points:",font="Arial 10 bold").grid(row=13,column=1,sticky=W)
		Label(self.adset_window,text="Peak criterion [MAX]:",font="Arial 10 bold").grid(row=15,column=1,sticky=W)


		#Containers
		self.new_fsr=StringVar()
		self.new_minV=StringVar()
		self.new_maxV=StringVar()
		self.new_master_wv=StringVar()
		self.new_rms_thr=StringVar()
		self.new_rms_points=StringVar()
		self.new_peak_crit=StringVar()


		self.new_fsr_entry=Entry(self.adset_window,textvariable=self.new_fsr,width=12)
		self.new_fsr_entry.grid(row=3,column=3)
		self.new_minV_entry=Entry(self.adset_window,textvariable=self.new_minV,width=12)
		self.new_minV_entry.grid(row=5,column=3)
		self.new_maxV_entry=Entry(self.adset_window,textvariable=self.new_maxV,width=12)
		self.new_maxV_entry.grid(row=7,column=3)
		self.new_master_wv_entry=Entry(self.adset_window,textvariable=self.new_master_wv,width=12)
		self.new_master_wv_entry.grid(row=9,column=3)
		self.new_rms_thr_entry=Entry(self.adset_window,textvariable=self.new_rms_thr,width=12)
		self.new_rms_thr_entry.grid(row=11,column=3)
		self.new_rms_points_entry=Entry(self.adset_window,textvariable=self.new_rms_points,width=12)
		self.new_rms_points_entry.grid(row=13,column=3)
		self.new_peak_crit_entry=Entry(self.adset_window,textvariable=self.new_peak_crit,width=12)
		self.new_peak_crit_entry.grid(row=15,column=3)

		#Here, we retreive variables from different classes and put them on the GUI
		Label(self.adset_window,text="{:.0f}".format(1000*self.lock._FSR)+" MHz",font="Arial 10").grid(row=3,column=5,sticky=E)
		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.daq_tasks.ao_scan.mn_voltage)+" V",font="Arial 10").grid(row=5,column=5,sticky=E)
		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.daq_tasks.ao_scan.mx_voltage)+" V",font="Arial 10").grid(row=7,column=5,sticky=E)
		Label(self.adset_window,text="{:.6f}".format(self.lock.get_master_wavelength())+ " nm",font="Arial 10").grid(row=9,column=5,sticky=E)
		Label(self.adset_window,text="{:.3f}".format(self.transfer_lock.master_rms_crit)+" ms",font="Arial 10").grid(row=11,column=5,sticky=E)
		Label(self.adset_window,text="{:.0f}".format(self.transfer_lock.rms_points),font="Arial 10").grid(row=13,column=5,sticky=E)
		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.master_peak_crit)+" MAX",font="Arial 10").grid(row=15,column=5,sticky=E)

		#Small frame for the buttons (Update,Cancel,Default)
		self.button_frame=Frame(self.adset_window)
		self.button_frame.grid(row=17,column=1,columnspan=5)

		self.button_frame.grid_columnconfigure(1,minsize=10)
		self.button_frame.grid_columnconfigure(3,minsize=10)

		Button(self.button_frame,command=self.update_adset_changes,text="Update",font="Arial 10 bold",width=15).grid(row=0,column=0,sticky=W)
		Button(self.button_frame,command=self.cancel_top,text="Cancel",font="Arial 10 bold",width=15).grid(row=0,column=2)
		Button(self.button_frame,command=self.default_adset,text="Set Default",font="Arial 10 bold",width=15).grid(row=0,column=4,sticky=E)

		self.adset_window.focus_force()


	"""
	The method below is exactly the same as the one before. However, this one allows to change addditional settings
	in/for the laser.
	"""
	def open_las_settings(self,ind):

		#Additional settnigs window - laser

		self.adset_window=Toplevel(self.parent,height=600,width=600)
		self.adset_window.title("Additional laser settings")
		self.adset_window.bind("<Escape>",self.cancel_top)


		self.adset_window.grid_columnconfigure(0,minsize=20)
		self.adset_window.grid_columnconfigure(2,minsize=30)
		self.adset_window.grid_columnconfigure(4,minsize=20)
		self.adset_window.grid_columnconfigure(6,minsize=20)

		self.adset_window.grid_rowconfigure(0,minsize=10)
		self.adset_window.grid_rowconfigure(2,minsize=5)
		self.adset_window.grid_rowconfigure(4,minsize=5)
		self.adset_window.grid_rowconfigure(6,minsize=5)
		self.adset_window.grid_rowconfigure(8,minsize=5)
		self.adset_window.grid_rowconfigure(10,minsize=5)
		self.adset_window.grid_rowconfigure(12,minsize=30)
		self.adset_window.grid_rowconfigure(14,minsize=10)



		Label(self.adset_window,text="Current Settings",font="Arial 10 bold").grid(row=1,column=5)

		Label(self.adset_window,text="Laser min. voltage [V]:",font="Arial 10 bold").grid(row=3,column=1,sticky=W)
		Label(self.adset_window,text="Laser min. voltage [V]:",font="Arial 10 bold").grid(row=5,column=1,sticky=W)
		Label(self.adset_window,text="Slave Laser Wavelength [nm]:",font="Arial 10 bold").grid(row=7,column=1,sticky=W)
		Label(self.adset_window,text="RMS Slave Lock Threshold [MHz]:",font="Arial 10 bold").grid(row=9,column=1,sticky=W)
		Label(self.adset_window,text="Peak criterion [MAX]:",font="Arial 10 bold").grid(row=11,column=1,sticky=W)



		self.new_minV=StringVar()
		self.new_maxV=StringVar()
		self.new_laser_wv=StringVar()
		self.new_rms_thr=StringVar()
		self.new_peak_crit=StringVar()


		self.new_minV_entry=Entry(self.adset_window,textvariable=self.new_minV,width=12)
		self.new_minV_entry.grid(row=3,column=3)
		self.new_maxV_entry=Entry(self.adset_window,textvariable=self.new_maxV,width=12)
		self.new_maxV_entry.grid(row=5,column=3)
		self.new_laser_wv_entry=Entry(self.adset_window,textvariable=self.new_laser_wv,width=12)
		self.new_laser_wv_entry.grid(row=7,column=3)
		self.new_rms_thr_entry=Entry(self.adset_window,textvariable=self.new_rms_thr,width=12)
		self.new_rms_thr_entry.grid(row=9,column=3)
		self.new_peak_crit_entry=Entry(self.adset_window,textvariable=self.new_peak_crit,width=12)
		self.new_peak_crit_entry.grid(row=11,column=3)


		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.daq_tasks.ao_laser.mn_voltages[ind]),font="Arial 10").grid(row=3,column=5,sticky=E)
		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.daq_tasks.ao_laser.mx_voltages[ind]),font="Arial 10").grid(row=5,column=5,sticky=E)
		Label(self.adset_window,text="{:.6f}".format(self.lock.get_slave_wavelength(ind))+ " nm",font="Arial 10").grid(row=7,column=5,sticky=E)
		Label(self.adset_window,text="{:.3f}".format(self.transfer_lock.slave_rms_crits[ind])+" MHz",font="Arial 10").grid(row=9,column=5,sticky=E)
		Label(self.adset_window,text="{:.2f}".format(self.transfer_lock.slave_peak_crits[ind])+" MAX",font="Arial 10").grid(row=11,column=5,sticky=E)


		self.button_frame=Frame(self.adset_window)
		self.button_frame.grid(row=13,column=1,columnspan=5)

		self.button_frame.grid_columnconfigure(1,minsize=10)
		self.button_frame.grid_columnconfigure(3,minsize=10)

		Button(self.button_frame,command=lambda: self.update_las_adset_changes(ind),text="Update",font="Arial 10 bold",width=15).grid(row=0,column=0,sticky=W)
		Button(self.button_frame,command=self.cancel_top,text="Cancel",font="Arial 10 bold",width=15).grid(row=0,column=2)
		Button(self.button_frame,command=lambda: self.default_las_adset(ind),text="Set Default",font="Arial 10 bold",width=15).grid(row=0,column=4,sticky=E)

		self.adset_window.focus_force()


	#Function that destroys the additional settings window if Cancel button is clicked (or Esc key)
	def cancel_top(self,event=None):
		if self.adset_window is not None:
			self.adset_window.destroy()
			self.adset_window=None

	
	#Function that destroys the channel selection window if Cancel button is clicked (or Esc key)
	def cancel_daqtop(self,event=None):
		if self.daqset_window is not None:
			self.daqset_window.destroy()
			self.daqset_window=None


	#Function that updates additional cavity settings
	def update_adset_changes(self):
		if self.adset_window is not None:

			try:
				fsr=float(self.new_fsr.get())
				self.lock.set_FSR(fsr)
			except ValueError:
				fsr=None
						
			try:
				mnv=float(self.new_minV.get())
			except ValueError:
				mnv=None
						
			try:
				mxv=float(self.new_maxV.get())
			except ValueError:
				mxv=None

			if mnv is None:
				if mxv is not None:
					self.transfer_lock.daq_tasks.ao_scan.configure_voltage_boundaries(self.transfer_lock.daq_tasks.ao_scan.mn_voltage,mxv)
			else:
				if mxv is not None:
					self.transfer_lock.daq_tasks.ao_scan.configure_voltage_boundaries(mnv,mxv)
				else:
					self.transfer_lock.daq_tasks.ao_scan.configure_voltage_boundaries(mnv,self.transfer_lock.daq_tasks.ao_scan.mx_voltage)

			try:
				mwv=float(self.new_master_wv.get())
				self.lock.set_master_frequency(mwv)
			except ValueError:
				mwv=None

			try:
				rmt=float(self.new_rms_thr.get())
				self.transfer_lock.master_rms_crit=rmt
			except ValueError:
				pass

			try:
				rmp=float(self.new_rms_points.get())
				self.transfer_lock.rms_points=rmp
			except ValueError:
				pass

			try:
				npc=float(self.new_peak_crit.get())
				self.transfer_lock.master_peak_crit=npc
			except ValueError:
				pass

			"""
			If certain parameters are changed, we also need to update parameters related to the lasers, such as 
			the adjusted FSR, and lockpoints.
			"""
			if mwv is not None or fsr is not None:
				self.lock.update_slave_FSRs()
				for i in range(2):
					self.laser_lckp[i].config(text='{:.0f}'.format(self.lock.get_laser_lockpoint(i)))
					self.adj_fsr[i].config(text='{:.1f}'.format(1000*self.lock._slave_FSR[i]))
					self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))


			#The window is destroyed at the end.
			self.adset_window.destroy()
			self.adset_window=None


	#Function that updates additional laser settings. "ind" determines which laser's settings are being changed.
	def update_las_adset_changes(self,ind):
		if self.adset_window is not None:

			try:
				mnv=float(self.new_minV.get())
			except ValueError:
				mnv=None
						
			try:
				mxv=float(self.new_maxV.get())
			except ValueError:
				mxv=None

			if mnv is None:
				if mxv is not None:
					self.transfer_lock.daq_tasks.ao_laser.configure_voltage_boundary(self.transfer_lock.daq_tasks.ao_laser.mn_voltages[ind],mxv,ind)
			else:
				if mxv is not None:
					self.transfer_lock.daq_tasks.ao_laser.configure_voltage_boundary(mnv,mxv,ind)
				else:
					self.transfer_lock.daq_tasks.ao_laser.configure_voltage_boundary(mnv,self.transfer_lock.daq_tasks.ao_laser.mx_voltages[ind],ind)


			try:
				swv=float(self.new_laser_wv.get())
				self.lock.set_slave_frequency(swv,ind)
			except ValueError:
				swv=None

			try:
				rmt=float(self.new_rms_thr.get())
				self.transfer_lock.slave_rms_crits[ind]=rmt
			except ValueError:
				pass

			try:
				npc=float(self.new_peak_crit.get())
				self.transfer_lock.slave_peak_crits[ind]=npc
			except ValueError:
				pass


			if swv is not None:
				self.laser_lckp[ind].config(text='{:.0f}'.format(self.lock.get_laser_lockpoint(ind)))
				self.adj_fsr[ind].config(text='{:.1f}'.format(1000*self.lock._slave_FSR[ind]))
				self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))

			self.adset_window.destroy()
			self.adset_window=None


	#Function that updates choice of DAQ channels to use.
	def update_daq_channels(self):

		try:
			sc_ao=self.new_scan_ao.get()
			l1_ao=self.new_las1_ao.get()
			l1_ai=self.new_las1_ai.get()
			m_ai=self.new_master_ai.get()
			if len(self.lasers)>1:
				l2_ao=self.new_las2_ao.get()
				l2_ai=self.new_las2_ai.get()
		except:
			raise ValueError("Something went wrong.")


		#It's important to check if there are two same channels chosen.
		if len(self.lasers)>1:
			if (sc_ao==l1_ao or sc_ao==l2_ao or l1_ao==l2_ao) or (m_ai==l1_ai or m_ai==l2_ai or l1_ai==l2_ai):
				raise Exception("Cannot use same channel for two devices.") #This will be caught by GUI logger.
			else:
				self.transfer_lock.daq_tasks.update_tasks([sc_ao,l1_ao,l2_ao],[m_ai,l1_ai,l2_ai])

		else:
			if sc_ao==l1_ao  or m_ai==l1_ai:
				raise Exception("Cannot use same channel for two devices.")
			else:
				self.transfer_lock.daq_tasks.update_tasks([sc_ao,l1_ao],[m_ai,l1_ai])

		#The window is destroyed at the end.
		self.cancel_daqtop()


	"""
	This method resets to the task created during initialization, i.e. if tasks were changed, this function will
	clear them and then re-create them using the config file used when opening the program.
	"""
	def reset_tasks(self):
		self.transfer_lock.daq_tasks.reset_tasks(self.default_cfg,len(self.lasers))
		self.cancel_daqtop()


	#Analogical method for the cavity/scan settings. 
	def default_adset(self):
		if self.adset_window is not None:
			self.lock.set_FSR(1000*float(self.default_cfg['CAVITY']['FSR']))
			self.lock.set_master_frequency(float(self.default_cfg['CAVITY']['Wavelength']))
			self.transfer_lock.master_rms_crit=float(self.default_cfg['CAVITY']['LockThreshold'])
			self.transfer_lock.rms_points=int(self.default_cfg['CAVITY']['RMS'])
			self.lock.update_slave_FSRs()
			self.transfer_lock.master_peak_crit=float(self.default_cfg['CAVITY']['PeakCriterion'])
			self.transfer_lock.daq_tasks.ao_scan.configure_voltage_boundaries(float(self.default_cfg['CAVITY']['MinVoltage']),float(self.default_cfg['CAVITY']['MaxVoltage']))

			self.adset_window.destroy()
			self.adset_window=None


	#And a similar method for laser settings.
	def default_las_adset(self,ind):
		if self.adset_window is not None:
			self.lock.slave_freqs[ind]=self.lock._def_slave_freqs[ind]
			self.transfer_lock.slave_rms_crits[ind]=float(self.default_cfg["LASER"+str(ind+1)]['LockThreshold'])
			self.transfer_lock.slave_peak_crits[ind]=float(self.default_cfg["LASER"+str(ind+1)]['PeakCriterion'])
			self.transfer_lock.daq_tasks.ao_laser.configure_voltage_boundary(float(self.default_cfg["LASER"+str(ind+1)]['MinVoltage']),float(self.default_cfg["LASER"+str(ind+1)]['MaxVoltage']),ind)
			self.adset_window.destroy()
			self.adset_window=None


	"""
	The method below updates scan parameters that are accessible from the main GUI. Most of them (apart from the 
	offset) can be accessed only if the scan is not running
	"""
	def update_scan_parameters(self):

		change=False

		try:
			sc_off=float(self.scan_off.get())		
			change=True
		except ValueError:
			sc_off=self.transfer_lock.daq_tasks.ao_scan.offset
				
		try:
			sc_a=float(self.scan_amp.get())
			change=True
		except ValueError:
			sc_a=self.transfer_lock.daq_tasks.ao_scan.amplitude
				
		try:
			sc_samp=float(self.samp_scan.get())
			change=True
			self.real_samp.config(text='{:.0f}'.format(sc_samp))
		except ValueError:
			sc_samp=self.transfer_lock.daq_tasks.ao_scan.n_samples
			
		try:
			sc_t=float(self.scan_t.get())
			change=True
			self.real_scfr.config(text='{:.1f}'.format(1000/sc_t))
		except ValueError:
			sc_t=self.transfer_lock.daq_tasks.ao_scan.scan_time

		if change:
			self.transfer_lock.daq_tasks.modify_scanning(sc_off,sc_a,sc_samp,sc_t)
			self.real_scoff.config(text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.offset))
			self.real_scst.config(text='{:.1f}'.format(1000*self.transfer_lock.daq_tasks.ao_scan.scan_step))
			self.real_scamp.config(text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.amplitude))
	

	#Special function for setting scan offset only.
	def set_scan_offset(self):
		try:
			sc_off=float(self.scan_off.get())
			self.transfer_lock.daq_tasks.ao_scan.set_offset(sc_off)
			self.real_scoff.config(text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.offset))

		except ValueError:
			pass


	#Special function to move the scan offset only.
	def move_scan_offset(self,x):
		self.transfer_lock.daq_tasks.ao_scan.move_offset(x)
		self.real_scoff.config(text='{:.2f}'.format(self.transfer_lock.daq_tasks.ao_scan.offset))


	#Method moving the cavity lock (of the master laser)
	def move_master_lck(self,num):
		self.lock.move_master_lockpoint(num)
		self.real_lckp.config(text='{:.0f}'.format(self.lock.master_lockpoint))


	#Method moving slave laser lockpoint.
	def move_slave_lck(self,num,ind):
		if self.lock is not None:
			self.lock.move_laser_lockpoint(num,ind)
			self.laser_lckp[ind].config(text='{:.0f}'.format(self.lock.get_laser_lockpoint(ind)))
			self.laser_r_lckp[ind].config(text="{:.3f}".format(self.lock.slave_lockpoints[ind]))


	#Method engaging lock for the cavity.
	def engage_cavity_lock(self):

		if self.running:
			
			self.cav_err_log_check.config(state="disabled")

			self.cav_lock_state.config(text="Engaged",fg="green")
			self.engage_lock_button.config(text="Disengage Lock",command=self.disengage_cavity_lock)

			self.transfer_lock.master_lock_engaged=True

			#If error logging is checked, we create an empty array.
			if self.cav_err_log.get():
				filename="./SWP/logs/logM"+datetime.datetime.fromtimestamp(time()).strftime('-%Y-%m-%d-%H.%M.%S')+".hdf5"
				self.master_f=h5py.File(filename,'w')

				self.master_error_log=self.master_f.create_dataset('Errors',(10**6,),maxshape=(None,),dtype='float16')
				self.master_time_log=self.master_f.create_dataset('Time',(10**6,),maxshape=(None,),dtype='float16')

				self.master_f.attrs['Lockpoint']=self.lock.master_lockpoint
				self.master_f.attrs['ScanAmplitude']=self.transfer_lock.daq_tasks.ao_scan.amplitude
				self.master_f.attrs['ScanTime']=self.transfer_lock.daq_tasks.ao_scan.scan_time
				self.master_f.attrs['Samples']=self.transfer_lock.daq_tasks.ao_scan.n_samples
				self.master_f.attrs['SamplingRate']=self.transfer_lock.daq_tasks.ao_scan.sample_rate
			
				self.mt_start=time()

				self.transfer_lock._master_counter=0

				self.master_logging_set=True
		

	#Method engaging lock for the slave laser. Possible only, if the cavity lock is already engaged. 
	def engage_laser_lock(self,ind,sweep=False):

		if self.running and self.transfer_lock.master_lock_engaged:

			self.las_err_log_check[ind].config(state="disabled")

			self.laser_lock_state[ind].config(text="Engaged",fg="green")

			self.transfer_lock.slave_locks_engaged[ind]=True

			if not sweep:
				self.sweep_start_entry[ind].config(state="disabled")
				self.sweep_stop_entry[ind].config(state="disabled")
				self.sweep_step_entry[ind].config(state="disabled")
				try:
					self.sweep_wait_entry[ind].config(state="disabled")
				except:
					self.sweep_speed_entry[ind].config(state="disabled")
				self.sweep_type_entry[ind].config(state="disabled")
				self.sw_button[ind].config(state="disabled")
				self.set_volt[ind].config(state="disabled")
				self.new_volt_entry[ind].config(state="disabled")
				self.engage_laser_lock_button[ind].config(text="Disengage Lock",command=lambda x=ind: self.disengage_laser_lock(x))

			if self.las_err_log[ind].get():
				filename="./SWP/logs/logS"+str(ind+1)+datetime.datetime.fromtimestamp(time()).strftime('-%Y-%m-%d-%H.%M.%S')+".hdf5"
				self.log_las_file[ind]=h5py.File(filename,'w')

				self.slave_err_log[ind]=self.log_las_file[ind].create_dataset('Errors',(10**6,),maxshape=(None,),dtype='float16')
				self.slave_time_log[ind]=self.log_las_file[ind].create_dataset('Time',(10**6,),maxshape=(None,),dtype='float16')
				self.slave_rfreq_log[ind]=self.log_las_file[ind].create_dataset('RealFrequency',(10**6,),maxshape=(None,),dtype='float16')
				self.slave_lfreq_log[ind]=self.log_las_file[ind].create_dataset('LockFrequency',(10**6,),maxshape=(None,),dtype='float16')
				self.slave_rr_log[ind]=self.log_las_file[ind].create_dataset('RealR',(10**6,),maxshape=(None,),dtype='float16')
				self.slave_lr_log[ind]=self.log_las_file[ind].create_dataset('LockR',(10**6,),maxshape=(None,),dtype='float16')

				self.log_las_file[ind].attrs['SetFrequency']=self.lasers[ind].get_set_frequency()

				self.lt_start[ind]=time()

				self.transfer_lock._slave_counters[ind]=0

				self.laser_logging_set[ind]=True

	"""
	Method called when cavity's lock is being disengaged. Note, that at the end it also automatically
	disengages slave laser locks. 
	"""
	def disengage_cavity_lock(self):

		self.master_locked_flag=False
		self.transfer_lock.master_lock_engaged=False

		self.cav_err_log_check.config(state="normal")

		self.twopeak_status_cv.itemconfig(self.twopeak_status,fill="red")
		self.cav_lock_status_cv.itemconfig(self.cav_lock_status,fill="red")

		self.cav_lock_state.config(text="Disengaged",fg="red")
		self.engage_lock_button.config(text="Engage Lock",command=self.engage_cavity_lock)

		
		#Some parameters are reset
		self.transfer_lock.master_err_history=deque(maxlen=self.transfer_lock._err_data_length)
		self.transfer_lock.master_err_history.append(0)
		self.transfer_lock.master_err_rms=0
		self.lock.master_err=0
		self.lock.master_err_prev=0
		self.lock.master_ctrl=0
		self.rms_cav.config(text="0")

		#If error signal logging was checked, the hdf5 file is closed.
		if self.cav_err_log.get():
			self.master_logging_set=False
			self.master_error_log.resize(self.transfer_lock._master_counter,axis=0)
			self.master_time_log.resize(self.transfer_lock._master_counter,axis=0)
			try:
				self.master_error_log[0]=self.master_error_log[1]
				self.master_time_log[0]=self.master_time_log[1]
			except:
				pass

			self.master_f.close()
			

		for i in range(len(self.lasers)):
			if self.transfer_lock.slave_locks_engaged[i]:
				self.disengage_laser_lock(i)


	#Method called when only one of slave laser's lock is being disengaged.
	def disengage_laser_lock(self,ind,sweep=False):

		self.las_err_log_check[ind].config(state="normal")

		self.laser_lock_state[ind].config(text="Disengaged",fg="red")

		self.transfer_lock.slave_locks_engaged[ind]=False
		self.transfer_lock.slave_locked_flags[ind].clear()

		self.laser_lock_status_cv[ind].itemconfig(self.laser_lock_status[ind],fill="red")

		self.transfer_lock.slave_err_history[ind]=deque(maxlen=self.transfer_lock._err_data_length)
		self.transfer_lock.slave_err_history[ind].append(0)
		self.transfer_lock.slave_err_rms[ind]=0
		self.lock.slave_errs[ind]=0
		self.lock.slave_errs_prev[ind]=0
		self.lock.slave_ctrls[ind]=0
		self.rms_laser[ind].config(text="0")



		if not sweep:
			self.sweep_start_entry[ind].config(state="normal")
			self.sweep_stop_entry[ind].config(state="normal")
			if self.sweep_type[ind].get()=="Discrete":
				self.sweep_step_entry[ind].config(state="normal")
			try:
				self.sweep_wait_entry[ind].config(state="normal")
			except:
				self.sweep_speed_entry[ind].config(state="normal")
			self.sweep_type_entry[ind].config(state="normal")
			self.sw_button[ind].config(state="normal")
			self.set_volt[ind].config(state="normal")
			self.new_volt_entry[ind].config(state="normal")
			self.engage_laser_lock_button[ind].config(text="Engage Lock",command=lambda x=ind:self.engage_laser_lock(x))

		#Again, error signal is logged if the option was chosen.
		if self.las_err_log[ind].get():
			self.laser_logging_set[ind]=False

			self.slave_err_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)
			self.slave_time_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)
			self.slave_rfreq_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)
			self.slave_lfreq_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)
			self.slave_rr_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)
			self.slave_lr_log[ind].resize(self.transfer_lock._slave_counters[ind],axis=0)

			try:
				self.slave_err_log[ind][0]=self.slave_err_log[ind][1]
				self.slave_time_log[ind][0]=self.slave_time_log[ind][1]
				self.slave_rfreq_log[ind][0]=self.slave_rfreq_log[ind][1]
				self.slave_lfreq_log[ind][0]=self.slave_lfreq_log[ind][1]
				self.slave_rr_log[ind][0]=self.slave_rr_log[ind][1]
				self.slave_lr_log[ind][0]=self.slave_lr_log[ind][1]
			except:
				pass

			self.log_las_file[ind].close()


	"""
	First function that is called when laser is swept. The sweep is operated in a separate thread and this function
	creates this thread (if previously used) and starts it running the sweep function. The thread is not daemon, so 
	it stops once the function is finished. Can only be called if the cavity lock is engaged. 
	"""
	def sweep_laser_th(self,ind):
		if self.transfer_lock.master_lock_engaged:
			try:
				self.sweep_thread[ind].start()
			except RuntimeError:
				self.sweep_thread[ind]=threading.Thread(target=self.sweep_laser,kwargs={"ind":ind})
				self.sweep_thread[ind].start()


	def conitnuous_sweep_th(self,ind):
		if self.transfer_lock.master_lock_engaged:
			self.cont_sweep_running=True
			try:
				self.cont_sweep_thread[ind].start()
			except RuntimeError:
				self.cont_sweep_thread[ind]=threading.Thread(target=self.cont_sweep_laser,kwargs={"ind":ind})
				self.cont_sweep_thread[ind].start()


	"""
	This the function that performs frequency sweep of the slave laser. It takes 4 parameters: start point, end point,
	step size and wait time. The first three are given in units of MHz deviation from R=0.5 lockpoint corresponding
	to 0 MHz, the last one in seconds. The sweep engages the lock and sets the lock point to the starting point. Then,
	it waits for the laser to lock, and after it is locked it waits "wait time" seconds until moving the locpoint by
	the "step size" amount. The process continues until the last step is reached or the process is stopped by the user,
	after which the laser lock is disengaged.
	"""
	def sweep_laser(self,ind):

		if self.transfer_lock.master_lock_engaged:

			#Getting parameters
			try:
				swstart=float(self.sweep_start[ind].get())
				if swstart<-self.lock._FSR*1000/2:
					swstart=-self.lock._FSR*1000/2
			except ValueError:
				return
						
			try:
				swstop=float(self.sweep_stop[ind].get())
				if swstop>self.lock._FSR*1000/2:
					swstop=self.lock._FSR*1000/2
			except ValueError:
				return
			
			if swstart==swstop:
				return
			elif swstart>swstop:
				swstart,swstop=swstop,swstart
				self.sweep_start[ind].set(swstart)
				self.sweep_stop[ind].set(swstop)

			try:
				swstep=abs(float(self.sweep_step[ind].get()))
				if swstep<1:
					swstep=1
				elif swstep>(swstop-swstart):
					swstep=swstop-swstart
				self.sweep_step[ind].set(swstep)
			except ValueError:
				return


			swwait=self.sweep_wait[ind].get()

			#Get all the lockpoints to use
			no_steps=1+math.ceil((swstop-swstart)/swstep)
			freqs=[]
			for i in range(no_steps):
				if swstart+i*swstep<=swstop:
					freqs.append(swstart+i*swstep)
				else:
					freqs.append(swstop)
					break

			#Disabling buttons and entry fields
			for j in range(len(self.lasers)):
				self.update_laser_lock_button[j].config(state="disabled")
				self.engage_laser_lock_button[j].config(state="disabled")
				self.las_err_log_check[j].config(state="disabled")
				self.minus10MHz[j].config(state="disabled")
				self.minus5MHz[j].config(state="disabled")
				self.minus1MHz[j].config(state="disabled")
				self.plus10MHz[j].config(state="disabled")
				self.plus5MHz[j].config(state="disabled")
				self.plus1MHz[j].config(state="disabled")
			self.minus10ms.config(state="disabled")
			self.minus5ms.config(state="disabled")
			self.minus1ms.config(state="disabled")
			self.plus10ms.config(state="disabled")
			self.plus5ms.config(state="disabled")
			self.plus1ms.config(state="disabled")

			if len(self.lasers)>1:
				if not self.transfer_lock.slave_locks_engaged[1-ind]:
					self.sw_button[1-ind].config(state="disabled")
			self.sw_button[ind].config(text="Stop",command=lambda: self.stop_sweep(ind))
			self.run_scan.config(state="disabled")
			self.update_lock.config(state="disabled")
			self.engage_lock_button.config(state="disabled")
			self.set_volt[ind].config(state="disabled")
			self.new_volt_entry[ind].config(state="disabled")
			self.sweep_type_entry[ind].config(state="disabled")
			self.move_offset_p.config(state="disabled")
			self.move_offset_m.config(state="disabled")
			self.set_offset.config(state="disabled")


			#Engaging the lock
			if not self.transfer_lock.slave_locks_engaged[ind]:
				self.engage_laser_lock(ind,sweep=True)

			#Sweep
			steps_done=0
			for fr in freqs:
				if self.stop_swp:
					self.stop_swp=False
					break
				self.transfer_lock.slave_locked_flags[ind].clear()
				self.transfer_lock.slave_lock_counters[ind]=0
				self.lock.set_laser_lockpoint(fr,ind)
				self.current_deviation[ind].config(text="{:.1f}".format(fr)+" MHz")
				self.current_dev_process[ind].config(text="Locking...")
				self.laser_lckp[ind].config(text='{:.0f}'.format(fr))
				self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))

				self.transfer_lock.slave_locked_flags[ind].wait()

				self.current_dev_process[ind].config(text="Waiting")

				sleep(swwait)

				steps_done+=1
				self.sw_pr_var[ind].set(steps_done/no_steps*100)

			#Disengage the lock when finished
			self.disengage_laser_lock(ind,sweep=True)


			#Bring back all the fields to normal
			for j in range(len(self.lasers)):
				self.update_laser_lock_button[j].config(state="normal")
				self.engage_laser_lock_button[j].config(state="normal")
				self.las_err_log_check[j].config(state="normal")
				self.minus10MHz[j].config(state="normal")
				self.minus5MHz[j].config(state="normal")
				self.minus1MHz[j].config(state="normal")
				self.plus10MHz[j].config(state="normal")
				self.plus5MHz[j].config(state="normal")
				self.plus1MHz[j].config(state="normal")
			self.minus10ms.config(state="normal")
			self.minus5ms.config(state="normal")
			self.minus1ms.config(state="normal")
			self.plus10ms.config(state="normal")
			self.plus5ms.config(state="normal")
			self.plus1ms.config(state="normal")
			self.run_scan.config(state="normal")
			self.update_lock.config(state="normal")
			self.engage_lock_button.config(state="normal")
			self.set_volt[ind].config(state="normal")
			self.new_volt_entry[ind].config(state="normal")
			self.sweep_type_entry[ind].config(state="normal")
			self.move_offset_p.config(state="normal")
			self.move_offset_m.config(state="normal")
			self.set_offset.config(state="normal")

			if len(self.lasers)>1:
				if not self.transfer_lock.slave_locks_engaged[1-ind]>1:
					self.sw_button[1-ind].config(state="normal")
			self.sw_button[ind].config(text="Sweep",command=lambda: self.sweep_laser_th(ind),state="normal")
			
			self.sw_pr_var[ind].set(0)
			
			self.current_deviation[ind].config(text="")
			self.current_dev_process[ind].config(text="")

	
	#Method invoked when user wants to stop frequency scan
	def stop_sweep(self,ind):
		self.sw_button[ind].config(text="Stopping...",state="disabled")
		self.stop_swp=True


	"""
	This the function that performs continuous frequency sweep of the slave laser. It takes as arguments start point,
	end point and sweep speed in MHz/s. It moves the laser lockpoint continuously (in reality at every loop iteration,
	which happen approximately every max(50ms,2*scan_time) due to "time.sleep()") by Speed*max(50,2*scan_time)/1000 MHz.
	"""
	def cont_sweep_laser(self,ind):

		if self.transfer_lock.master_lock_engaged:

			#Getting parameters
			try:
				swstart=float(self.sweep_start[ind].get())
				if swstart<-self.lock._FSR*1000/2:
					swstart=-self.lock._FSR*1000/2
			except ValueError:
				return
						
			try:
				swstop=float(self.sweep_stop[ind].get())
				if swstop>self.lock._FSR*1000/2:
					swstop=self.lock._FSR*1000/2
			except ValueError:
				return
			
			if swstart==swstop:
				return
			elif swstart>swstop:
				swstart,swstop=swstop,swstart
				self.sweep_start[ind].set(swstart)
				self.sweep_stop[ind].set(swstop)


			swspd=self.sweep_speed[ind].get()
			wait=max(50,2*self.transfer_lock.daq_tasks.ao_scan.scan_time)/1000 #Wait time in seconds
			step=swspd*wait
			
			interval=swstop-swstart
			current=swstart

			increasing=True

			#Disabling buttons and entry fields
			for j in range(len(self.lasers)):
				self.update_laser_lock_button[j].config(state="disabled")
				self.engage_laser_lock_button[j].config(state="disabled")
				self.las_err_log_check[j].config(state="disabled")
				self.minus10MHz[j].config(state="disabled")
				self.minus5MHz[j].config(state="disabled")
				self.minus1MHz[j].config(state="disabled")
				self.plus10MHz[j].config(state="disabled")
				self.plus5MHz[j].config(state="disabled")
				self.plus1MHz[j].config(state="disabled")
			self.minus10ms.config(state="disabled")
			self.minus5ms.config(state="disabled")
			self.minus1ms.config(state="disabled")
			self.plus10ms.config(state="disabled")
			self.plus5ms.config(state="disabled")
			self.plus1ms.config(state="disabled")

			if len(self.lasers)>1:
				if not self.transfer_lock.slave_locks_engaged[1-ind]:
					self.sw_button[1-ind].config(state="disabled")
			self.sw_button[ind].config(text="Stop",command=lambda x=ind: self.stop_cont_sweep(x))
			self.run_scan.config(state="disabled")
			self.update_lock.config(state="disabled")
			self.engage_lock_button.config(state="disabled")
			self.set_volt[ind].config(state="disabled")
			self.new_volt_entry[ind].config(state="disabled")
			self.sweep_type_entry[ind].config(state="disabled")
			self.sweep_speed_entry[ind].config(state="disabled")
			self.move_offset_p.config(state="disabled")
			self.move_offset_m.config(state="disabled")
			self.set_offset.config(state="disabled")


			#Engaging the lock
			if not self.transfer_lock.slave_locks_engaged[ind]:
				self.engage_laser_lock(ind,sweep=True)

			self.lock.set_laser_lockpoint(swstart,ind)
			self.laser_lckp[ind].config(text='{:.0f}'.format(current))
			self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))
			self.transfer_lock.slave_locked_flags[ind].wait(60)


			#Sweep
			while self.cont_sweep_running:
				if increasing:
					if current+step>swstop:
						increasing=False
						current=swstop
						self.lock.set_laser_lockpoint(swstop,ind)
						self.current_deviation[ind].config(text="{:.3f}".format(current)+" MHz")
						self.laser_lckp[ind].config(text='{:.0f}'.format(current))
						self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))
						self.sw_pr_var[ind].set((current-swstart)/interval*100)
						self.transfer_lock.slave_locked_flags[ind].clear()
						self.transfer_lock.slave_lock_counters[ind]=0
						self.transfer_lock.slave_locked_flags[ind].wait()
					else:
						self.lock.move_laser_lockpoint(step,ind)
						current+=step

					self.current_dev_process[ind].config(text="Increasing")

				else:
					if current-step<swstart:
						increasing=True
						current=swstart
						self.lock.set_laser_lockpoint(swstart,ind)
						self.current_deviation[ind].config(text="{:.3f}".format(current)+" MHz")
						self.laser_lckp[ind].config(text='{:.0f}'.format(current))
						self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))
						self.sw_pr_var[ind].set((current-swstart)/interval*100)
						self.transfer_lock.slave_locked_flags[ind].clear()
						self.transfer_lock.slave_lock_counters[ind]=0
						self.transfer_lock.slave_locked_flags[ind].wait()
					else:
						self.lock.move_laser_lockpoint(-step,ind)
						current-=step

					self.current_dev_process[ind].config(text="Decreasing")


				self.current_deviation[ind].config(text="{:.3f}".format(current)+" MHz")
				self.laser_lckp[ind].config(text='{:.0f}'.format(current))
				self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))
				self.sw_pr_var[ind].set((current-swstart)/interval*100)

				sleep(wait)

				
			#Disengage the lock when finished
			self.disengage_laser_lock(ind,sweep=True)


			#Bring back all the fields to normal
			for j in range(len(self.lasers)):
				self.update_laser_lock_button[j].config(state="normal")
				self.engage_laser_lock_button[j].config(state="normal")
				self.las_err_log_check[j].config(state="normal")
				self.minus10MHz[j].config(state="normal")
				self.minus5MHz[j].config(state="normal")
				self.minus1MHz[j].config(state="normal")
				self.plus10MHz[j].config(state="normal")
				self.plus5MHz[j].config(state="normal")
				self.plus1MHz[j].config(state="normal")
			self.minus10ms.config(state="normal")
			self.minus5ms.config(state="normal")
			self.minus1ms.config(state="normal")
			self.plus10ms.config(state="normal")
			self.plus5ms.config(state="normal")
			self.plus1ms.config(state="normal")
			self.run_scan.config(state="normal")
			self.update_lock.config(state="normal")
			self.engage_lock_button.config(state="normal")
			self.set_volt[ind].config(state="normal")
			self.new_volt_entry[ind].config(state="normal")
			self.sweep_type_entry[ind].config(state="normal")
			self.sweep_speed_entry[ind].config(state="normal")
			self.move_offset_p.config(state="normal")
			self.move_offset_m.config(state="normal")
			self.set_offset.config(state="normal")

			if len(self.lasers)>1:
				if not self.transfer_lock.slave_locks_engaged[1-ind]>1:
					self.sw_button[1-ind].config(state="normal")

			self.sw_button[ind].config(text="Sweep",command=lambda: self.conitnuous_sweep_th(ind),state="normal")
			
			self.sw_pr_var[ind].set(0)
			
			self.current_deviation[ind].config(text="")
			self.current_dev_process[ind].config(text="")

	
	#Method invoked when user wants to stop frequency scan
	def stop_cont_sweep(self,ind):
		self.sw_button[ind].config(text="Stopping...",state="disabled")
		self.cont_sweep_running=False


	#Method letting the user to directly set voltage on the laser.
	def set_voltage(self,ind):
		try:
			v=float(self.new_volt[ind].get())

			volts=self.transfer_lock.daq_tasks.ao_laser.voltages
			volts[ind]=v
			self.transfer_lock.daq_tasks.set_laser_volts(volts)

			self.app_volt[ind].config(text='{:.3f}'.format(self.transfer_lock.daq_tasks.ao_laser.voltages[ind]))

		except ValueError:
			pass

	
	#Method called when "Update Lock" button is clicked for the cavity. 
	def update_master_lock(self):

		try:
			pg=float(self.P_gain.get())
			self.lock.prop_gain[0]=pg
			self.real_pg.config(text='{:.3f}'.format(pg))
		except ValueError:
			pass
					
		try:
			ig=float(self.I_gain.get())
			self.lock.int_gain[0]=ig
			self.real_ig.config(text='{:.3f}'.format(ig))
		except ValueError:
			pass
					
		try:
			stp=float(self.lck_stp.get())
			self.lock.set_master_lockpoint(stp)
			self.real_lckp.config(text='{:.1f}'.format(stp))
		except ValueError:
			pass


	#Method called when "Update Lock" button is clicked for a slave laser.
	def update_laser_lock(self,ind):

		try:
			pg=float(self.laser_P[ind].get())
			self.lock.prop_gain[ind+1]=pg
			self.laser_pg[ind].config(text='{:.3f}'.format(pg))
		except ValueError:
			pass
					
		try:
			ig=float(self.laser_I[ind].get())
			self.lock.int_gain[ind+1]=ig
			self.laser_ig[ind].config(text='{:.3f}'.format(ig))
		except ValueError:
			pass
					
		try:
			stp=float(self.laser_lsp[ind].get())
			self.lock.set_laser_lockpoint(stp,ind)
			self.laser_lckp[ind].config(text='{:.0f}'.format(self.lock.get_laser_lockpoint(ind)))
			self.laser_r_lckp[ind].config(text='{:.3f}'.format(self.lock.slave_lockpoints[ind]))
		except ValueError:
			pass


	#Method that begins the scan. The scan happenes in a separate thread and this function creates it and starts it.	
	def start_scanning(self):

		#Disablign some buttons and entry fields
		for i in range(len(self.laser_settings)):
			self.laser_settings[i].config(state="disabled")

		self.update_scan.config(state="disabled")
		self.scan_t_entry.config(state="disabled")
		self.samp_scan_entry.config(state="disabled")
		self.scan_amp_entry.config(state="disabled")
		self.cav_settings.config(state="disabled")
		self.change_channels.config(state="disabled")
		
		#Changing flags
		self.transfer_lock.start_scan()
		self.run_scan.configure(text="Stop Scanning",command=self.stop_scanning)
		self.running=True
		

		"""
		Creating threads. The function responsible for scanning and acquiring data obtains this whole class (or rather 
		its object) as one of its arguments to actively perform changes to GUI and plot. 
		"""
		try:
			if self.transfer_lock._scan_thread is None:
				self.transfer_lock._scan_thread=threading.Thread(target=self.transfer_lock.scan,kwargs={"GUI_object":self})
			self.transfer_lock._scan_thread.start()

		except RuntimeError:
			self.transfer_lock._scan_thread=threading.Thread(target=self.transfer_lock.scan,kwargs={"GUI_object":self})
			self.transfer_lock._scan_thread.start()


	#Method called when the scan is paused/stopped. It also disengages all the locks. 
	def stop_scanning(self):

		self.transfer_lock.stop_scan()
		self.running=False

		self.update_scan.config(state="normal")
		self.run_scan.configure(text="Start Scanning",command=self.start_scanning)
		self.scan_t_entry.config(state="normal")
		self.samp_scan_entry.config(state="normal")
		self.scan_amp_entry.config(state="normal")
		self.cav_settings.config(state="normal")
		self.change_channels.config(state="normal")
		for i in range(len(self.lasers)):
			self.laser_settings[i].config(state="normal")

		
		self.disengage_cavity_lock()


#################################################################################################################
	
"""
The class below takse care of the plotting window. It is divided into 4 plots: the first one plots data acquired from
the photodetectors and shows the current signals with peaks, the three other plots show real-time error signal of the
master laser lock and both slave laser locks. The length of the data used for error signal is set to 100 points. For
all the plots the scales are automatically adjusted. This class contains no methods.
"""
class PlotWindow:
	def __init__(self,parent):

		self.parent=parent

		parent.grid_columnconfigure(0,minsize=2)
		parent.grid_columnconfigure(2,minsize=2)
		parent.grid_rowconfigure(0,minsize=2)
		parent.grid_rowconfigure(2,minsize=2)

		#Plotting frame 
		self.plot_frame=Frame(parent,width=610)
		self.plot_frame.grid(row=1,column=1,sticky=NE)


		#Defining a figure
		self.fig=plt.figure(figsize=(5.8,6.7),dpi=100)
		self.fig.patch.set_facecolor("#F0F0ED")
		self.fig_grid=GridSpec(6,1,hspace=0.4,left=0.08,right=0.99,top=0.99,bottom=0.06)
		self.fig_grid.update()
		self.ax=plt.subplot(self.fig_grid[:3,0])
		self.ax.set_xlim(0,150)
		self.ax.set_ylim(0,1.1)
		self.ax.autoscale(enable=True,axis='both')


		#The fastest way to redraw plots is by setting data on 2D lines that we initialize here
		self.msline,=self.ax.plot([],[],'b-',lw=1)
		self.lline1,=self.ax.plot([],[],'g-',lw=1)
		self.lline2,=self.ax.plot([],[],'r-',lw=1)
		self.mvline,=self.ax.plot([],[],'c--',lw=1)
		self.lvline1,=self.ax.plot([],[],color="lime",linestyle='--',lw=1)
		self.lvline2,=self.ax.plot([],[],color="pink",linestyle='--',lw=1)

		self.all_lines=[self.msline,self.lline1,self.lline2,self.mvline,self.lvline1,self.lvline2]

		self.plot_frame.grid_columnconfigure(0, minsize=5)
		self.plot_frame.grid_columnconfigure(2, minsize=5)
		self.plot_frame.grid_rowconfigure(0,minsize=2)
		self.plot_frame.grid_rowconfigure(2,minsize=2)

		#To embed the figure in TkInter GUI we need to create a frame with a canvas in it, which will contain the figure.
		self.scan_plot=Frame(self.plot_frame)
		self.scan_plot.grid(row=1,column=1,sticky=NE)

		self.canvas=FigureCanvasTkAgg(self.fig,self.scan_plot)
		self.canvas.draw()
		self.canvas.get_tk_widget().pack(side=TOP, fill=BOTH, expand=True)


		#Figure's subplots for error plotting
		self.ax_err=plt.subplot(self.fig_grid[3,0])
		self.ax_err.tick_params(labelbottom=False)
		self.mline,=self.ax_err.plot([],[],'k-',lw=1)
		self.ax_err_L=[None,None]
		self.ax_err_L[0]=plt.subplot(self.fig_grid[4,0])
		self.ax_err_L[0].tick_params(labelbottom=False)
		self.ax_err_L[1]=plt.subplot(self.fig_grid[5,0])
		self.ax_err_L[1].tick_params(labelbottom=False)
		# self.ax_err_L[0].set_ylabel('MHz')
		# self.ax_err_L[1].set_ylabel('MHz')
		# self.ax_err.set_ylabel('ms')
		self.slines=[None,None]
		for i in range(2):
			self.slines[i],=self.ax_err_L[i].plot([],[],'k-',lw=1)
		


#################################################################################################################
"""
This class is called when the program is initialized. It is responsible for the left-most pane and it, if necessary,
allows to choose lasers to include out of the list of available lasers, allows to choose a config file, displays
short error messages when caught, as well as short information about the connected lasers.
"""
class LaserConnect:
	def __init__(self,parent,pane,trans_frame,plot_frame,simulate):

		self.pane=pane
		self.parent=parent
		self.laser_tabs=[]
		self.status=[]
		self.trans_frame=trans_frame
		self.plot_frame=plot_frame
		self.sim=simulate

		parent.grid_columnconfigure(2,minsize=10)
		parent.grid_columnconfigure(0,minsize=10)
		parent.grid_rowconfigure(0,minsize=50)
		parent.grid_rowconfigure(2,minsize=50)
		parent.grid_rowconfigure(4,minsize=50)
		parent.grid_rowconfigure(5,minsize=50)
		parent.grid_rowconfigure(6,minsize=50)
		parent.grid_rowconfigure(7,minsize=50)
		parent.grid_rowconfigure(8,minsize=50)
		parent.grid_rowconfigure(9,minsize=300)
		parent.grid_rowconfigure(10,minsize=100)

		self.cfg_label=Label(parent,text="Config settings:",font="Arial 10 bold")
		self.cfg_label.grid(row=2,column=1,sticky=S)

		#The defualt configuration is chosen initially. 
		self.cfg_file=StringVar()
		self.cfg_file.set(os.path.dirname(os.path.realpath(__file__))+"/configs/DEFAULT.ini")

		self.cfg_filename=Label(parent,wraplengt=175,text="DEFAULT")
		self.cfg_filename.grid(row=3,column=1,sticky=N)

		self.cfg_button=Button(parent,text="Choose config",width=15,command=self.dialbox)
		self.cfg_button.grid(row=4,column=1)

		
		self.caught_err=Label(parent,text="",wraplengt=175)
		self.caught_err.grid(row=10,column=1)

		self.con_button=Button(parent, text="Connect Lasers",width=13,height=5,font="Arial 14 bold",command=self.initialize)
		self.con_button.grid(row=1,column=1,sticky=W)


	#This method is called when user wants to choose a differnt config file.
	def dialbox(self):
		new_f=filedialog.askopenfilename(initialdir = os.path.dirname(os.path.realpath(__file__))+"/configs",title = "Select file",filetypes = (("config files","*.ini"),))
		if new_f=="":
			pass
		else:	
			self.cfg_file.set(new_f)
			self.cfg_filename.configure(text=os.path.split(self.cfg_file.get())[1][:-4])

		self.parent.lift()

		
	#Method that's called to initialize the rest of GUI and connect the lasers.
	def initialize(self,L=None):

		if L is None:

			self.cfg_label.destroy()
			self.cfg_button.destroy()
			self.cfg_filename.destroy()

			self.config=load_conf(self.cfg_file.get())

			L=connect_lasers()
			

		else:
			self.lab0.destroy()
			self.lab1.destroy()
			self.lab2.destroy()
			self.las1_opt.destroy()
			self.las2_opt.destroy()


		if len(L)==0:
			self.caught_err.configure(text="Didn't find any devices \n connected to the computer")

		elif len(L)<3:
			self.caught_err.configure(text="")
			tab_ctrl=ttk.Notebook(self.pane)
			tabs=[]
			las=[]
			for i in range(len(L)):
				tabs.append(ttk.Frame(tab_ctrl))
				self.add_status(2*i+4,i+1)
			for i in range(len(L)):
				tab_ctrl.add(tabs[i], text="Laser "+str(i+1))
				try:
					cfg_wvl=float(self.config['LASER'+str(i+1)]['Wavelength'])
					las.append(LaserControl(tabs[i],self.status[i],L[i],cfg_wvl))
				except ValueError:
					las.append(LaserControl(tabs[i],self.status[i],L[i]))
			

			self.laser_tabs=las #Every laser gets its own tab

			tab_ctrl.pack(expand=1,fill=BOTH)

			self.con_button.configure(state="disabled")

			pw=PlotWindow(self.plot_frame)
			self.TC=TransferCavity(self.trans_frame,pw,L,self.config,self.sim)

		else:
			#If there are more than 2 lasers connected to the computer, user has to choose 2 from the list.

			self.err.configure(text="More than two devices \n have been detected. \n Please choose up to 2 \n to connect.")
			self.con_button.configure(state="disabled")

			self.lab0=Label(self.parent,text="Choose lasers:",font="Arial 10 bold")
			self.lab0.grid(row=5,column=1)
			self.lab1=Label(self.parent,text="Laser 1:",font="Arial 10")
			self.lab1.grid(row=6,column=1,sticky=N)
			self.lab2=Label(self.parent,text="Laser 2:",font="Arial 10")
			self.lab2.grid(row=8,column=1,sticky=N)

			self.Llist=[str(l) for l in L]
			self.Lrem=self.Llist+["None"]

			self.las1=StringVar()
			self.las1_opt=OptionMenu(self.parent,self.las1,*self.Llist)
			self.las1_opt.grid(row=6,column=1,sticky=S)
			self.las1.set("None")
			self.las1.trace("w",self.laser_choice_update)

			self.las2=StringVar()
			self.las2_opt=OptionMenu(self.parent,self.las2,*self.Lrem)
			self.las2_opt.grid(row=8,column=1,sticky=S)
			self.las2.set("None")
			self.las2_opt.config(state="disabled")
			self.las2.trace("w",self.laser_choice_update)

			self.L_to_connect=[]
			self.L=L


	#Helper function updating choice lists.
	def laser_choice_update(self,*args):
		if self.las1.get()!="None":
			self.L_to_connect=[l for l in self.L if str(l)==self.las1.get() or str(l)==self.las2.get()]
			self.con_button.config(state="normal",command=lambda:self.initialize(L=self.L_to_connect))
			self.las2_opt.config(state="normal")
			self.las2.set("None")


	#Helper function creating small indicators
	def add_status(self,rw,ind):

		cl=Label(self.parent,text="Laser "+str(ind),font="Arial 12 bold")
		cl.grid(row=rw,column=1,sticky=W)
		cv=Canvas(self.parent,height=30,width=30)
		cv.grid(row=rw,column=1,sticky=E)
		ov=cv.create_oval(5,5,25,25,fill="red")
		wl=Label(self.parent,text="\u03bb:",font="Arial 10 bold")
		wl.grid(row=rw+1,column=1,sticky=W)
		wv=Label(self.parent,font="Arial 10 bold",text="")
		wv.grid(row=rw+1,column=1,sticky=E)
		self.status.append((cv,ov,wv))



