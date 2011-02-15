#!/usr/bin/python
import xml.parsers.expat
import asyncore
import asynchat
import argparse
import socket
import errno
import time
import ssl
import sys
import re

debug = False
VERSION = 0.1

class NZBParser():
	"""
	Crude NZB Parser.
	We initialize a few things for tracking stats in here, as well as making
	huge arrays of things we probably don't need.
	"""
	def __init__(self):
		self.parser = xml.parsers.expat.ParserCreate()
		self.parser.StartElementHandler = self.start_element
		self.parser.EndElementHandler = self.end_element
		self.parser.CharacterDataHandler = self.char_data
		self.pattern = re.compile(r' "(.*)" yEnc ')
		# Storage of results.
		self.results = dict()
		self.results["totalFiles"] = 0
		self.results["totalBytes"] = 0
		self.results["totalArticles"] = 0
		self.results["missingArticles"] = 0
		self.results["files"] = dict()

	def start_element(self, name, attrs):
		"""
		Beginning of XML element.
		"""
		if debug: print "Element: '%s'" % (name)
		if debug: print "\tAttrs: %s" % (attrs)
		self.current_data = ''
		if name == "file":
			self.results["totalFiles"] = self.results["totalFiles"] + 1
			# Extract the filename, make new entries for it in the results.
			# We really don't need filenames.
			self.curFile = str(self.pattern.search(attrs["subject"]).groups())
			self.results["files"][self.curFile] = dict()
			self.results["files"][self.curFile]["groups"] = list()
			self.results["files"][self.curFile]["segments"] = list()
		if name == "segment":
			self.results["totalBytes"] = self.results["totalBytes"] + int(attrs["bytes"])
			self.results["totalArticles"] = self.results["totalArticles"] + 1

	def end_element(self, name):
		"""
		End of XML element
		"""
		if debug: print "Ended element: '%s'" % (name)
		if name == "file":
			return
		if name == "segment":
			if debug: print "Added segment: "+self.current_data
			self.current_data.strip()
			self.results["files"][self.curFile]["segments"].append(str(self.current_data))
		if name == "group":
			self.current_data.strip()
			self.results["files"][self.curFile]["groups"].append(str(self.current_data))

	def char_data(self, data):
		"""
		Buffer the XML data until the end of the element.
		"""
		self.current_data = self.current_data + data

	def parse(self, filename):
		"""
		Open the given file and parse it.
		"""
		try:
			fh = open(filename)
		except:
			print "ERROR: Could not open NZB file '%s'" % (filename)
			sys.exit(1)

		self.fh = fh
		self.parser.ParseFile(fh)
		# Build an easily iterable dict/array from the stuff we took out of the nzb
		self.results["groups"] = dict()
		for filename in self.results["files"]:
			for group in self.results["files"][filename]["groups"]:
				if not group in self.results["groups"]:
					self.results["groups"][group] = list()
				self.results["groups"][group].extend(self.results["files"][filename]["segments"])
		fh.close()

		return self.results

class async_chat_ssl(asynchat.async_chat):
	"""
	Class to make wrap asynchat in SSL
	"""
	def connect(self, host, use_ssl=False):
		"""
		If we're connecting with SSL, set the socket to blocking.
		"""
		self.use_ssl = use_ssl
		if use_ssl:
			self.socket.setblocking(1)
		asynchat.async_chat.connect(self, host)

	def handle_connect(self):
		"""
		Wrap the socket with SSL when we connect.
		"""
		if self.use_ssl:
			self.ssl = ssl.wrap_socket(self.socket)
			self.set_socket(self.ssl)

	def push(self, data):
		"""
		Small wrapper for the push method, so that we can print
		out what we're pushing in debug mode.
		"""
		if debug: print data
		asynchat.async_chat.push(self, data + "\r\n")

class NZBHandler(async_chat_ssl):
	def __init__(self, config, nzbdata):
		# asynchat
		asynchat.async_chat.__init__(self)
		self.set_terminator("\r\n")
		self.data = ""
		# Config
		self.conf = config
		self.nzbdata = nzbdata
		# Status
		self.authed = False
		self.curgrp = None
		self.working = None
		self.finished = False
		self.segiter = None
		self.groupiter = None
		self.groupname = None

	# NNTP commands that we need to send
	def group(self, groupname):
		"""
		Change to a new newsgroup
		"""
		self.working = True
		self.push("GROUP " + groupname)
		self.changed = groupname

	def noop(self):
		"""
		We use the "DATE" command to do nothing, since it's cheap.
		"""
		self.working = True
		self.push("DATE")

	def stat(self, article):
		"""
		STAT an article in the active newsgroup
		"""
		self.working = True
		self.push("STAT <" + article + ">")

	def quit(self):
		"""
		QUIT and disconnect from the server.
		"""
		self.working = True
		self.push("QUIT")

	# Methods for handling NNTP responses
	# DATE response.
	def response_111(self):
		self.working = False

	# Welcome banner. We send our username.
	def response_200(self):
		if self.conf.username:
			self.push("AUTHINFO USER %s" % self.conf.username)

	# Disconnecting.
	def response_205(self):
		if debug: print "205: Disconnecting."
		self.close()
		self.finished = True

	# Group switched successfully.
	def response_211(self):
		if debug: print "211: Group switched."
		self.curgrp = self.changed
		self.working = False

	# Article exists
	def response_223(self):
		if debug: print "223: Article exists."
		self.working = False

	# Authenticated successfully.
	def response_281(self):
		if debug: print "281: Authentication successful."
		self.authed = True

	# Request for further authentication, send password.
	def response_381(self):
		self.push("AUTHINFO PASS %s" % self.conf.password)

	# Non-existant group
	def response_411(self):
		if debug: print "411: Group does not exist."
		self.working = False

	# No such article number in this group
	def response_423(self):
		if debug: print "423: No such article in this group."
		self.working = False
		self.nzbdata["missingArticles"] = self.nzbdata["missingArticles"] + 1

	# No such article found
	def response_430(self):
		if debug: print "430: No such article found."
		self.working = False
		self.nzbdata["missingArticles"] = self.nzbdata["missingArticles"] + 1

	# Authentication failed. We'll quit if we hit this.
	def response_452(self):
		if debug: print "452: Authentication failed."
		self.working = False
		self.quit()

	# Command not recognised. We'll quit if we hit this.
	def response_500(self):
		if debug: print "500: Command not recognised."
		if debug: print "Command was: '%s'." % self.lastcommand
		self.working = False
		self.quit()

	# Buffer incoming data until we get the terminator
	def collect_incoming_data(self, data):
		"""
		Buffer the incoming data.
		"""
		self.data = self.data + data

	def found_terminator(self):
		"""
		Called when we find the terminator in the incoming data
		"""
		if debug: print self.data
		nntpcode = self.data[0:3]

		# Call the appropriate method
		m = 'response_' + str(nntpcode)
		if hasattr(self, m):
			getattr(self, m)()
		else:
			print "Handler not found for response '%s'. Quitting." % nntpcode
			self.quit()

		if self.finished:
			return

		# OK, here we actually do the work of switching groups and checking articles.
		if self.authed and not self.working:
			if not self.groupiter:
				self.groupiter = self.nzbdata["groups"].__iter__()
			if not self.groupname:
				try:
					self.groupname = self.groupiter.next()
				except StopIteration:
					self.groupname = None
					self.quit()
			if self.groupname == self.curgrp:
				if not self.segiter:
					self.segiter = self.nzbdata["groups"][self.groupname].__iter__()
				try:
					segment = self.segiter.next()
				except StopIteration:
					self.groupname = None
					segment = None
					self.noop()
				if segment:
					self.stat(segment)
			elif self.groupname != None:
				self.group(self.groupname)

		# Clear out the data ready for the next run
		self.data = ""

	def run(self, host, port, sslmode):
		self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
		self.connect((host, port), use_ssl = sslmode)
		asyncore.loop()

def getopts():
	argparser = argparse.ArgumentParser(
		description = "Check article completion on an NNTP server",
		epilog = "Note: The checking may take a while to complete, depending on the number of articles."
	)
	argparser.add_argument(
		'-s', '--server',
		type = str,
		required = True,
		help = "Server to connect to.",
	)
	argparser.add_argument(
		'-p', '--port',
		type = int,
		default = 119,
		help = "Specify the port that the server is listening on.",
	)
	argparser.add_argument(
		'-S', '--ssl',
		type = bool,
		metavar = '[0,1]',
		default = False,
		help = "Enable SSL mode.",
	)
	argparser.add_argument(
		'-u', '--username',
		type = str,
		help = "Username to authenticate as.",
	)
	argparser.add_argument(
		'-P', '--password',
		type = str,
		help = "Password to authenticate with.",
	)
	argparser.add_argument(
		'-f', '--filename',
		type = str,
		required = True,
		help = "NZB file to check.",
	)
	argparser.add_argument(
		'-d', '--debug',
		type = bool,
		metavar = '[0,1]',
		default = False,
		help = "Enable verbose output.",
	)
	argparser.add_argument(
		'-v', '--version',
		action = 'version',
		version = '%(prog)s v' + str(VERSION),
		help = "Show version information.",
	)

	return argparser.parse_args()

if __name__ == '__main__':
	conf = getopts()
	if conf.debug:
		debug = True
	nzbparser = NZBParser()
	results = nzbparser.parse(conf.filename)
	print ""
	print "NZB Parsing Results"
	print "==================="
	print "Totals"
	print "\tFiles: %s" % (results["totalFiles"])
	print "\tArticles: %s" % (results["totalArticles"])
	print "\tArticle Bytes: %s" % (results["totalBytes"])
	print ""
	
	print "Be patient, this might take a while :)"
	nzbhandler = NZBHandler(conf, results)
	nzbhandler.run(conf.server, conf.port, conf.ssl)
	
	print ""
	print "NZB Checking Results"
	print "===================="
	print "Totals"
	print "\tMissing Articles: %s" % (nzbhandler.nzbdata["missingArticles"])
	print "\tCompletion: %.2f%%" % (((nzbhandler.nzbdata["totalArticles"] - nzbhandler.nzbdata["missingArticles"]) / nzbhandler.nzbdata["totalArticles"]) * 100)
	print ""
	print "All done!"
