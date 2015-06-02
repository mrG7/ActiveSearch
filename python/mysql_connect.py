#!/usr/bin/python2.7
from __future__ import division
import MySQLdb
import scipy.sparse as ss
import numpy as np, numpy.linalg as nlg

from nltk.stem.snowball import SnowballStemmer
import gaussianRandomFeatures as grf
import time
import re
import string
import sys

from multiprocessing import Process, Queue


def mysql_connect(database, db_host="localhost", db_user="root", db_password=""):
	db = MySQLdb.connect(host=db_host,
			     user=db_user,
			     passwd=db_password,
			     db=database)
	return db

# retrieve the tf_idf frequencies from the database
# return a scipy.sparse.csr_matrix of the form [wordID][messageID] = count of times word seen in that message
def getTFIDFSimilarity(db):
	num_words = getTotalWordCount(db)
	num_messages = getTotalMessageCount(db)

	cur = db.cursor()
	cur.execute("SELECT * FROM tf_idf_dictionary ORDER BY messageid")

	ret_row = []
	ret_col = []
	ret_data = []

	for row in cur.fetchall():
		wordID = row[0]
		currentMessageID = row[1]
		count = row[2]
		ret_row.append(wordID)
		ret_col.append(currentMessageID)
		ret_data.append(count)

	ret_matrix = ss.csr_matrix((ret_data, (ret_row, ret_col)), shape=(num_words, num_messages))
	return ret_matrix
	
# getTFIDFSimilarity retrieves the pre-processed similarity from the database. Building the database
# is slow. This function builds the TFIDF in memory here which is about 5x faster but has to be done
# every time the daemon is started
def getTFIDFSimilarityFromMessage(db, tfidf_wordlimit, number_of_threads, hostname, username, password, database,skip_stemmer):
	# This code can remove words seen in more than some % of the messages
	# It turns out this is not very useful in the datasets that we have so 
	# the functionality hasn't been implemented in the Perl import code yet
	stopword_threshold = 0.95


	total_words = 0

	message_count = int(getTotalMessageCount(db))

	# the total number of times a word was seen
	# a word being in this dict will determine if it is used
	# if a word is removed from this dict it will not be used
	wordcount = {}
	# the number of messages that this word was in
	wordcount_message = {}
	# the number of times a word was seen in a given message
	emailwords = []

	thread_range_messages = int(message_count / number_of_threads)
	# round up so the last thread doesn't drop messages
	if ((message_count % number_of_threads) != 0):
		thread_range_messages += 1
	print "range is ", thread_range_messages
	mythreads = []
	myqueues = []
	for i in range(number_of_threads):
		start_message = thread_range_messages * i
		end_message = min(thread_range_messages * (i+1), message_count)
		# range is [start_message, end_message)
		print "Thread ", start_message, " to ", end_message
		q = Queue()
		t = Process(target=calculateTFIDF, args=(start_message,end_message, q, hostname, username, password, database, i, skip_stemmer))
		t.start()
		mythreads.append(t)
		myqueues.append(q)

	for i in range(number_of_threads):
		mycount = myqueues[i].get()
		print "array size " , mycount
		for j in range(mycount):
			mydict = myqueues[i].get()
			emailwords.append(mydict)
		wordcount_local = myqueues[i].get()
		for word in wordcount_local:
			total_words += wordcount_local[word]
			if (word in wordcount):
				wordcount[word] += wordcount_local[word]
			else:
				wordcount[word] = wordcount_local[word]
		wordcount_message_local = myqueues[i].get()
		for word in wordcount_message_local:
			if (word in wordcount_message):
				wordcount_message[word] += wordcount_message_local[word]
			else:
				wordcount_message[word] = wordcount_message_local[word]
		mythreads[i].join()
	print "Threads done"

	# We used to count the words all at once here in the main thread but it is
	# 3% to 6% faster to do it in the thread and send it over a pipe because
	# most threads can do their counting while waiting for the slowest thread to
	# finish doing its main processing. 
	print "Total words seen ", total_words

	stopword_threshold_count = int(stopword_threshold * float(message_count))
	print "\nRemoving words seen in more than " + str(stopword_threshold_count) + " messages"
	stopword_removed_count = 0
	for word in wordcount_message:
		if (wordcount_message[word] > stopword_threshold_count):
			print "Removing high frequency word ", word
			del wordcount[word]
			stopword_removed_count += 1
	print "Removed ", stopword_removed_count

	if (tfidf_wordlimit > 0):
		print "\nCalculating wordcount threshold"
		wordcount_threshold = 1
		while (len(wordcount) > tfidf_wordlimit):
			if (wordcount_threshold % 10 == 0):
				sys.stdout.write('.')
			for word in wordcount.keys():
				if (wordcount[word] < wordcount_threshold):
					del wordcount[word]
			wordcount_threshold += 1
		print "\nWordcount threshold was " + str(wordcount_threshold) + ". " + (str(len(wordcount))) + " words remain"

	word_id_next = 0
	word_id_list = {}
	ret_row = []
	ret_col = []
	ret_data = []

	for messageid in range(message_count):
		for word in emailwords[messageid].keys():
			if (word not in wordcount):
				continue

			word_id = -1
			if (word in word_id_list):
				word_id = word_id_list[word]
			else:
				word_id = word_id_next
				word_id_list[word] = word_id
				word_id_next += 1

			ret_row.append(word_id)
			ret_col.append(messageid)
			ret_data.append(emailwords[messageid][word])

	ret_matrix = ss.csr_matrix((ret_data, (ret_row, ret_col)), shape=(len(wordcount), message_count))
	return ret_matrix


# return the number of words in the tf_idf computation
def getTotalWordCount(db):
	cur = db.cursor()
	cur.execute("SELECT COUNT(word_id) FROM tf_idf_wordmap")

	row=cur.fetchone()
	return str(row[0])

# return the number of messages
def getTotalMessageCount(db):
	cur = db.cursor()
	cur.execute("SELECT COUNT(messageID) FROM messages")

	row=cur.fetchone()
	return str(row[0])

# return the number of users
def getTotalUserCount(db):
	cur = db.cursor()
	cur.execute("SELECT COUNT(psersonid) FROM people")

	row=cur.fetchone()
	return str(row[0])

# for a given message id, return an array of recipients of that message
# each value is a long, not a string
def getRecipientsByMessage(message_id, db):
	cur = db.cursor()
	cur.execute("SELECT DISTINCT personid FROM recipients WHERE messageid=" + str(message_id))
	data = []

	for row in cur.fetchall():
		data.append(row[0])

	return data

# for a given a keyword, return an array of "<message_id> : <timestamp> : <subject>"
# for any message whose subject matches 
# we do not scrub the inputs here and assume the user won't do anything malicious
def getMessagesByKeywordSubject(word, db):

	cur = db.cursor()
	cur.execute("SELECT messages.messageid, messages.messagedt, messages.subject"
				+ " FROM messages WHERE subject LIKE '%" + word + "%' ORDER BY messagedt")
	data = []

	for row in cur.fetchall():
		data.append(str(row[0]) + " : " + str(row[1]) + " : " + row[2])

	return data

# for a given a keyword, return an array of "<message_id> : <timestamp> : <subject>"
# for any message whose body
# we do not scrub the inputs here and assume the user won't do anything malicious
def getMessagesByKeyword(word, db):
	cur = db.cursor()
	cur.execute("SELECT messages.messageid, messages.messagedt, messages.subject"
				+ " FROM messages INNER JOIN bodies ON messages.messageid=bodies.messageid "
				+ " WHERE body LIKE '%" + word + "%' ORDER BY messages.messagedt")
	data = []

	for row in cur.fetchall():
		data.append(str(row[0]) + " : " + str(row[1]) + " : " + row[2])

	return data

# for a message_id, return the subject string
def getSubjectByMessage(message_id, db):
	return getFieldByMessage(message_id, "subject", db)

# for a message_id, return the timestamp
def getTimeByMessage(message_id, db):
	return str(getFieldByMessage(message_id, "messagedt", db)) + ""

# for a message_id, return the sender_id
def getSenderByMessage(message_id, db):
	return getFieldByMessage(message_id, "senderid", db)

# for a message_id, return a specific field
def getFieldByMessage(message_id, field_name, db):
	cur = db.cursor()
	cur.execute("SELECT " + field_name + " FROM messages WHERE messageid = " + str(message_id) + " LIMIT 1")

	row=cur.fetchone()
	return row[0]

# return an array listing all people involved in a message: the sender and the recipients
# If the sender is negative (culled but frequency threshold) don't return it
def getUsersByMessage(message_id, db):
	ret_array = getRecipientsByMessage(message_id, db)
	sender = getSenderByMessage(message_id, db)
	if (sender >= 0):
		ret_array.append(sender)

	return ret_array

# returns an array where each value is a string of the form "<message_id> <seconds from epoch timestamp>""
def getMessageTimes(db):
	cur = db.cursor()
	cur.execute("SELECT messageid, UNIX_TIMESTAMP(messagedt) FROM messages")
	data = []

	for row in cur.fetchall():
		data.append((str(row[0]), float(row[1])))

	return data


# returns an array where each value is a string of the form "<message_id> <seconds from epoch timestamp> <sender_id>"
def getMessageTimesAndSenders(db):
	cur = db.cursor()
	cur.execute("SELECT messageid, UNIX_TIMESTAMP(messagedt), senderid FROM messages")
	data = []

	for row in cur.fetchall():
		data.append(str(row[0]) + " " + str(row[1]) + " " + row[2])

	return data

# returns an array where the first element is the message timestamp, the second is the subject, and the third is the body
def getMessageBodyFromMessageID(message_id, db):
	cur = db.cursor()
	cur.execute("SELECT messages.messagedt, messages.subject, bodies.body FROM messages, bodies"
				+ " WHERE messages.messageid=bodies.messageid AND messages.messageid=" + str(message_id))

	row=cur.fetchone()
	data = []
	data.append(row[0])
	data.append(row[1])
	data.append(row[2])

	return data

# returns a the subject string for a message_id
def getMessageSubjectFromMessageID(message_id, db):
	cur = db.cursor()
	cur.execute("SELECT messages.subject FROM messages WHERE messages.messageid=" + str(message_id))

	row=cur.fetchone()
	return row[0]

# for user_ids user_from and user_to, return an array where each value is a message that user_from sent and user_to received
# Each value is of the form "<message_id> : <timestamp> : <subject>"
def getMessagesFromUserToUser(user_from, user_to, db):
	cur = db.cursor()
	cur.execute("SELECT messages.messageid, messages.messagedt, messages.subject"
				+ " FROM messages, recipients WHERE messages.messageid=recipients.messageid AND recipients.personid="
				+ str(user_to) + " AND messages.senderid=" + str(user_from)
				+ " GROUP BY messages.messageid ORDER BY messages.messagedt")
	data = []

	for row in cur.fetchall():
		data.append(str(row[0]) + " : " + str(row[1]) + " : " + row[2])

	return data

#returns the username string from a user_id
#If no name is available, return the email address instead
def getUserNameFromID(user_id, db):
	cur = db.cursor()
	cur.execute("SELECT * FROM people WHERE personid=" + str(user_id))

	row=cur.fetchone()
	if (row is None):
		return str(user_id) + " "

	email_address = row[1]
	name = row[2]

	if (name is None or len(name) == 0):
		return email_address

	return name


randomFeatures = None
ts_magic_number = 13168189440000.0
rn = 100
sine=True
rfc = None

def getTimeSimMatrix (db):
	global rfc

	if rfc is None:
		randomFeatures = grf.GaussianRandomFeatures(dim=2,gammak=1/ts_magic_number,rn=rn,sine=True)

	tdata = getMessageTimes(db)

	ret_row = []
	ret_col = []

	for t in tdata:

		rf = randomFeatures.computeRandomFeatures (t[1])

		for idx,v in enumerate([rf]):
			ret_col.append(t[0])
			ret_row.append(idx)
			ret_data.append(v)

	num_f = rn*2 if sine else rn
	ret_matrix = ss.csr_matrix((ret_data, (ret_row, ret_col)), shape=(num_f, getTotalMessageCount(db)))
	return ret_matrix


def getSenderSimMatrix (db):
	# sdata = getMessageTimes(db)
	emailCount = getTotalMessageCount(db)

	ret_row = []
	ret_col = []
	

	for i in xrange(emailCount):
		user_list = getUsersByMessage(i, db)
		ret_row.extend(user_list)
		ret_col.extend(len(user_list) * [i])

	ret_data = len(ret_row) * [1]

	ret_matrix = ss.csr_matrix((ret_data, (ret_row, ret_col)), shape=(getTotalUserCount(db), emailCount))
	return ret_matrix

def getWordMatrix(db, tfidf_wordlimit, dotfidf,skip_stemmer, num_threads, hostname, username, password, database):
	similarity_data = None
	if (dotfidf):
		t1 = time.time()
		similarity_data = getTFIDFSimilarityFromMessage(db, tfidf_wordlimit, num_threads, hostname, username, password, database,skip_stemmer)
		print("Time for importing data ", time.time() - t1)
	else:
		print "Skipping tfidf computation and reading it from the database instead"
		similarity_data = getTFIDFSimilarity(db)
	print "one"
	s = 1./(np.sqrt((similarity_data.multiply(similarity_data)).sum(1)))
#	print s.shape
#	print similarity_data.shape
#      print type(similarity_data)
#	print type(s)		

#	import IPython
#	IPython.embed()

	s[np.isinf(s)] == 0
	s = ss.csr_matrix(s)

	similarity_data = similarity_data.multiply(s)
	return similarity_data

def getFinalFeatureMatrix (db, tfidf_wordlimit, dotfidf,skip_stemmer, num_threads, hostname, username, password, database, tc=1.0, sc=1.0):
	# if not using any of these matrices, remove them from the calculation to save computation of zeros

	wMat = getWordMatrix(db, tfidf_wordlimit, dotfidf,skip_stemmer, num_threads, hostname, username, password, database)

	if (tc > 0):
		tMat = getTimeSimMatrix (db)
		wMat = ss.bmat([[wMat],[tc*tMat]])
	if (sc > 0):
		sMat = getSenderSimMatrix (db)
		wMat = ss.bmat([[wMat],[sc*sMat]])
	
	# import IPython
	# IPython.embed()
	# The next two lines of code remove rows/columns of wMat which 
	# are entirely only 0.
	wMat = wMat[np.squeeze(np.array(np.nonzero(wMat.sum(axis=1))[0])),:]
	wMat = wMat[:,np.squeeze(np.array(np.nonzero(wMat.sum(axis=0))))[1]]

	return wMat


def getAffinityMatrix (db, tfidf_wordlimit, dotfidf,skip_stemmer, num_threads, hostname, username, password, database, tc=1.0, sc=1.0):
	# if not using any of these matrices, remove them from the calculation to save computation of zeros
	
	wMat = getFinalFeatureMatrix(db, tfidf_wordlimit, dotfidf,skip_stemmer, num_threads, hostname, username, password, database, tc, sc)
	return wMat.T.dot(wMat)

def calculateTFIDF(start_message, end_message, myqueue, hostname, username, password, database, thread_id, skip_stemmer):
	db = mysql_connect(database, hostname, username, password)

	emailwords = [dict() for x in range(end_message - start_message)]

	skip_words = {'the': 1}
	skip_words['the'] = 1
	skip_words['be'] = 1
	skip_words['to'] = 1
	skip_words['of'] = 1
	skip_words['and'] = 1
	skip_words['a'] = 1
	skip_words['in'] = 1
	skip_words['that'] = 1
	skip_words['have'] = 1
	skip_words['i'] = 1
	skip_words['it'] = 1
	skip_words['for'] = 1
	skip_words['not'] = 1
	skip_words['on'] = 1
	skip_words['with'] = 1
	skip_words['he'] = 1
	skip_words['as'] = 1
	skip_words['you'] = 1
	skip_words['do'] = 1
	skip_words['at'] = 1

	skip_words['this'] = 1
	skip_words['but'] = 1
	skip_words['his'] = 1
	skip_words['by'] = 1
	skip_words['from'] = 1
	skip_words['they'] = 1
	skip_words['we'] = 1
	skip_words['say'] = 1
	skip_words['her'] = 1
	skip_words['she'] = 1
	skip_words['or'] = 1
	skip_words['an'] = 1
	skip_words['will'] = 1
	skip_words['my'] = 1
	skip_words['one'] = 1
	skip_words['all'] = 1
	skip_words['would'] = 1
	skip_words['there'] = 1
	skip_words['their'] = 1
	skip_words['what'] = 1

	# the total number of times a word was seen
	# a word being in this dict will determine if it is used
	# if a word is removed from this dict it will not be used
	wordcount = {}
	# the number of messages that this word was in
	wordcount_message = {}


	message_count = end_message - start_message
	local_message_id = 0

	stemmer = SnowballStemmer("english")
	
	prev_msg = -1

	cur = db.cursor()
	cur.execute("SELECT * FROM bodies WHERE messageid >=" + str(start_message) + " AND messageid <" + str(end_message) + " ORDER BY messageid")
	# loop through the messages and track the words seen
	for message in cur.fetchall():
		messageid = message[0]
		body = message[1]
		if (prev_msg >= 0 and (prev_msg+1) != messageid):
			print "OKOK ", prev_msg, " and ", messageid
		prev_msg = messageid

		message_arr = re.split('\s+', string.lower(body))

		if (local_message_id % 1000 == 0):
			print "Thread ",thread_id, " ", local_message_id, " / ", message_count
		for word in message_arr:
			# remove nonword characters
			word = re.sub('[\W_]+', '', word)
			if (word == ""):
				continue
			if (len(word) > 255):
				continue
			if (word in skip_words):
				continue
			if (skip_stemmer == False):
				try:
					word = stemmer.stem(word)
				except:
					print "Stemming error in word ", word, " message ", messageid

				# save the count of this word in this message
#			if (local_message_id not in emailwords):
#				emailwords[local_message_id] = {}
			if (word in emailwords[local_message_id]):
				emailwords[local_message_id][word] += 1
			else:
				emailwords[local_message_id][word] = 1

		local_message_id += 1

	print "Thread ", thread_id, ": Counting words"
	# count the total number of times each word was seen
	for messageid in range(local_message_id):
		for word in emailwords[messageid].keys():
			if (word in wordcount):
				wordcount[word] += emailwords[messageid][word]
			else:
				wordcount[word] = emailwords[messageid][word]
			if (emailwords[messageid][word] > 0):
				if (word in wordcount_message):
					wordcount_message[word] += 1
				else:
					wordcount_message[word] = 1

	if (len(emailwords) != (end_message - start_message)):
		print "Error: Thread ", thread_id, ":emailwords array size (", len(emailwords), ") does not match expected number of words: ", (end_message - start_message)
		sys.exit()
	if (local_message_id != (end_message - start_message)):
		print "Error: Thread ", thread_id, ":local_message_id (", local_message_id, ") does not match expected number of words: ", (end_message - start_message)
		sys.exit()
	myqueue.put(end_message - start_message)
	for i in range(end_message - start_message):
	     myqueue.put(emailwords[i])
	myqueue.put(wordcount)
	myqueue.put(wordcount_message)
