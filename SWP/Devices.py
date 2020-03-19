from time import sleep, time
from math import ceil
from .NKTP_DLL import *
from .Registry import REG


"""
Classes in this file are responsible for controlling the laser. These are basically
functions from the provided NKT DLL. The appropriate hex adressess are taken from
the Registry file.
"""


#Class representing the laser. Methods are self-explanatory.
class Laser:

	def __init__(self, port, dev_address):

		self.port=port
		self.devID=dev_address

	def __str__(self):
		try:
			name=self.get_name()
		except:
			name=''

		return self.port+": device "+str(self.devID)+", "+name

	def get_central_wavelength(self):
		resc,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		return center*0.0001

	def get_wavelength(self):

		resc,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		reso,offset=registerReadS16(self.port,self.devID,REG['Current_offset'],-1)

		return (center+offset)*0.0001

	def get_frequency(self):

		c=299792.458

		resc,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		reso,offset=registerReadS16(self.port,self.devID,REG['Current_offset'],-1)

		return c/(0.0001*(center+offset))

	def get_set_wavelength(self):

		resc,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		reso,offset=registerReadS16(self.port,self.devID,REG['Wavelength_offset'],-1)

		return (center+offset)*0.0001

	def get_set_frequency(self):

		c=299792.458

		resc,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		reso,offset=registerReadS16(self.port,self.devID,REG['Wavelength_offset'],-1)

		return c/(0.0001*(center+offset))

	def set_wavelength(self,wavelength):

		res,center=registerReadU32(self.port,self.devID,REG['Wavelength_center'],-1)
		center*=0.0001

		offset=int(round((wavelength-center)*10000))

		if offset>3700:
			offset=3700
		elif offset<-3700:
			offset=-3700

		wrRes=registerWriteS16(self.port,self.devID,REG['Wavelength_offset'],offset,-1)

	def set_frequency(self,frequency):
		c=299792.458
		self.set_wavelength(c/frequency)

	def move_frequency(self,deviation):

		self.set_frequency(self.get_set_frequency()+deviation/1000)

	def get_temperature(self):

		res,tp=registerReadS16(self.port,self.devID,REG['Temperature'],-1)

		return tp*0.1

	def get_power(self):

		res,pw=registerReadU16(self.port,self.devID,REG['Output_power'],-1)

		return pw*0.01

	def emission_on(self):

		emR = registerWriteU8(self.port,self.devID, REG['Emission'], 1, -1)

	def emission_off(self):

		emR = registerWriteU8(self.port,self.devID, REG['Emission'], 0, -1)

	def is_on(self):

		res,val=registerReadU8(self.port,self.devID,REG['Status'],-1)
		val='{0:016b}'.format(val)[::-1]
		val=int(val[0])
		return val

	def modulation_type(self,setting):

		rd,val=registerReadU16(self.port,self.devID,REG['Setup'],-1)
		val=list('{0:010b}'.format(val))
		if val[-2]!=str(setting):
			val[-2]=str(setting)
			val=''.join(val)
			val=int(val,2)
			wr=registerWriteU16(self.port,self.devID,REG['Setup'],val,-1)


	def get_modulation_type(self):
		rd,val=registerReadU16(self.port,self.devID,REG['Setup'],-1)
		val=list('{0:010b}'.format(val))

		if int(val[-2])==0:
			return "Wide"
		else:
			return "Narrow"

	def error_readout(self):
		rd,val=registerReadU16(self.port,self.devID,REG['Status'],-1)
		val='{0:016b}'.format(val)[::-1]
		val=int(val[-1])
		return val

	def get_name(self):
		rd,val=registerReadAscii(self.port,self.devID,REG['Name'],-1)
		return val.decode('utf-8')

	def write_name(self,name):
		rd=registerWriteAscii(self.port,self.devID,REG['Name'],name,0,-1)


#################################################################################################################


#Class created just to rename an exception.
class DeviceError(Exception):
	pass


#################################################################################################################


#Helper class used to choose laser out of connected devices.
class Device:

	def __init__(self,dvType,dvID,prt):
		self.devType=dvType
		self.devID=dvID
		self.port=prt
		self.laser=0

	def make_laser(self):

		if self.devType=="0x33" and self.laser==0:
			self.laser=Laser(self.port,self.devID)
			return self.laser


#################################################################################################################


#Helper class used to obtain list of devices from all open ports.
class Port:

	def __init__(self,portname):

		self.name=portname
		self.devices=[]

	def create_devices(self):

		res,deviceList=deviceGetAllTypes(self.name)

		for devID in range(0,len(deviceList)):
			if (deviceList[devID]!=0):
				d=Device(hex(deviceList[devID]),devID,self.name)
				self.devices.append(d)

	def close_port(self):
		clR=closePorts(self.name)


#################################################################################################################

"""
Global functions. First one opens all the ports and creates appropriate objects and returns a list with them.
The second one first creates devices connected to the ports and then returns list of lasers found amongst the
devices at all ports.
"""

def create_ports():
	roP=openPorts(getAllPorts(), 1, 1)
	P=[]
	for portname in getOpenPorts().split(','):
		P.append(Port(portname))

	return P

def connect_lasers():

	openports=create_ports()

	L=[]

	for port in openports:
		port.create_devices()
		for device in port.devices:
			las=device.make_laser()
			if las!=0 and las!=None:
				L.append(las)

	return L
