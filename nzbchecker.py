#!/usr/bin/env python2
from setproctitle import setproctitle
from pynzb import nzb_parser
import asyncore
import asynchat
import argparse
import socket
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
		self.results = {
			"totalBytes" : 0,
			"totalFiles" : 0,
			"totalArticles" : 0,
			"nzbdata" : None,
		}

	def parse(self, filename):
		"""
		Open the given file and parse it.
		"""
		try:
			fh = open(filename)
		except:
			print "ERROR: Could not open NZB file '%s'" % (filename)
			sys.exit(1)

		nzbxml = fh.read()
		fh.close()

		nzbdata = nzb_parser.parse(nzbxml)
		for nzb in nzbdata:
			self.results["totalFiles"] += 1
			for segment in nzb.segments:
				self.results["totalArticles"] += 1
				self.results["totalBytes"] += segment.bytes
		self.results["nzbdata"] = nzbdata
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
		self.nzbdata = nzbdata["nzbdata"]
		self.remaining = nzbdata["totalArticles"]
		self.totalArticles = nzbdata["totalArticles"]
		self.missing = 0
		# Status
		self.authed = False
		self.curgrp = None
		self.working = None
		self.finished = False
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
		self.missing += 1

	# No such article found
	def response_430(self):
		if debug: print "430: No such article found."
		self.working = False
		self.missing += 1

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

	# Access restriction/permission denied
	def response_502(self):
		if debug: print "502: Access restriction."
		self.working = False
		self.quit()

	# Get the next message_id from the nzbdata
	def get_message_id(self):
		message_id = None
		if len(self.nzbdata[0].segments) > 0:
			segment = self.nzbdata[0].segments.pop()
			self.groupname = self.nzbdata[0].groups[0]
			message_id = segment.message_id
		else:
			self.nzbdata.pop(0)
			self.groupname = None
			message_id = ""

	# Buffer incoming data until we get the terminator
	def collect_incoming_data(self, data):
		"""
		Buffer the incoming data.
		"""
		self.data += data

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

		if len(self.nzbdata) == 0:
			self.quit()
		# OK, here we actually do the work of switching groups and checking articles.
		if self.authed and not self.working:
			message_id = None
			if len(self.nzbdata[0].segments) > 0:
				segment = self.nzbdata[0].segments.pop()
				self.groupname = self.nzbdata[0].groups[0]
				message_id = segment.message_id
			else:
				self.nzbdata.pop(0)
				self.noop()
				self.groupname = None

			if message_id:
				msg = "Remaining: %s" % (self.remaining)
				if debug:
					print msg
				else:
					print " " * (len(msg) + 1),
					print "\r%s" % (msg),
					sys.stdout.flush()
				self.stat(message_id)
				self.remaining -= 1

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

def pretty_size(size, real=True):
	suffixes = {
		1000: [
			'KB', 'MB', 'GB',
			'TB', 'PB', 'EB',
			'ZB', 'YB'
		],
		1024: [
			'KiB', 'MiB', 'GiB',
			'TiB', 'PiB', 'EiB',
			'ZiB', 'YiB'
		],
	}

	multiple = 1024 if real else 1000

	for suffix in suffixes[multiple]:
		size /= multiple
		if size < multiple:
			return '{0:.1f} {1}'.format(size, suffix)

if __name__ == '__main__':
	setproctitle(sys.argv[0])
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
	print "\tArticle Size: %s" % (pretty_size(results["totalBytes"]))
	print ""
	
	print "Be patient, this might take a while :)"
	nzbhandler = NZBHandler(conf, results)
	nzbhandler.run(conf.server, conf.port, conf.ssl)

	print "\n"
	print "NZB Checking Results"
	print "===================="
	print "Totals"
	print "\tMissing Articles: %s" % (nzbhandler.missing)
	print "\tCompletion: %.2f%%" % (100 - (nzbhandler.missing / float(nzbhandler.totalArticles) * 100))
	print ""
	print "All done!"
