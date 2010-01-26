#!/usr/bin/env python2.6
import sys, logging, threading, os, atexit, time, optparse
from datetime import datetime
import itertools, traceback, json
sys.path.append(os.path.join(os.path.dirname(__file__), '../..'))

#from ngt.protocols import * # <-- dotdict, pack, unpack
import ngt.protocols as protocols
from ngt.protocols import protobuf, dotdict, rpc_services
from ngt.utils.containers import LockingOrderedSet

from ngt.messaging import messagebus
from ngt.messaging.messagebus import MessageBus
from amqplib.client_0_8 import Message
logger = logging.getLogger('dispatch')
logger.setLevel(logging.INFO)
#logging.getLogger().setLevel(logging.DEBUG)
#logging.getLogger('protocol').setLevel(logging.DEBUG)



mb = MessageBus()

sys.path.insert(0, '../..')
from django.core.management import setup_environ
from ngt import settings
setup_environ(settings)
from models import Reaper
from ngt.jobs.models import Job, JobSet
from django.db.models import Q
from commands import jobcommands


command_map = {
    'registerReaper': 'register_reaper',
    'unregisterReaper': 'unregister_reaper',
    'getJob': 'get_next_job',
    'jobStarted': 'job_started',
    'jobEnded': 'job_ended',
    'shutdown': '_shutdown',
}


JOB_RELEASE_LIMIT = 100
dblock = threading.Lock()

def create_jobcommand_map():
    ''' Create a map of command names to JobCommand subclasses from the jobcommand module '''
    jobcommand_map = {}
    # [type(getattr(jobcommands,o)) == type and issubclass(getattr(jobcommands,o), jobcommands.JobCommand) for o in dir(jobcommands)]
    for name in dir(jobcommands):
        obj = getattr(jobcommands, name)
        if type(obj) == type and issubclass(obj, jobcommands.JobCommand):
            if obj.name in jobcommand_map:
                raise ValueError("Duplicate jobcommand name: %s" % obj.name)
            jobcommand_map[obj.name] = obj
    return jobcommand_map
jobcommand_map = create_jobcommand_map()
logger.debug("jobcommand_map initialized: %s" % str(jobcommand_map))
logger.debug("Valid jobcommands:")
for k in jobcommand_map.keys():
    logger.debug(k)



###
# COMMANDS
###

def register_reaper(msgbytes):
    # TODO: Handle the corner case where a reaper has been expired or soft-deleted, and tries to register itself again.
    # Currently this would result in a ProgrammerError from psycopg
    request = protocols.unpack(protobuf.ReaperRegistrationRequest, msgbytes)
    
    logger.info("Got registration request from reaper %s" % request.reaper_uuid)
    try:
        r = Reaper.objects.get(uuid=request.reaper_uuid) # will get deleted or expired reapers, too
        logger.info("Reaper %s exists.  Resurrecting." % request.reaper_uuid[:8])
        if 'hostname' in request:
            r.hostname = request.hostname
        r.deleted = False
        r.expired = False
        dblock.acquire()
        r.save()
        dblock.release()
    except Reaper.DoesNotExist:
        r = Reaper(uuid=request.reaper_uuid, type=request.reaper_type)
        if 'hostname' in request:
            r.hostname = request.hostname
        dblock.acquire()
        r.save()
        dblock.release()
        logger.info("Registered reaper: %s" % request.reaper_uuid)
    return protocols.pack(protobuf.AckResponse, {'ack': protobuf.AckResponse.ACK})

def unregister_reaper(msgbytes):
    request = protocols.unpack(protobuf.ReaperUnregistrationRequest, msgbytes)
    try:
        r = Reaper.objects.get(uuid=request.reaper_uuid)
        r.soft_delete()
        logger.info("Reaper deleted: %s" % request.reaper_uuid)
    except Reaper.DoesNotExist:
        logger.error("Tried to delete an unregistered reaper, UUID %s" % reaper_uuid)
    return protocols.pack(protobuf.AckResponse, {'ack': protobuf.AckResponse.ACK})

####
# Job Fetching Logic
####



def check_readiness(job):
    '''Return True if the job is ready to be processed, False otherwise.'''
    if not job.dependencies_met():
        logger.debug("Job %s(%s) has unmet dependencies." % (job.uuid[:8], job.command)) 
        return False
    if job.command in jobcommand_map:
        return jobcommand_map[job.command].check_readiness(job)
    else:
        return True
    
def preprocess_job(job):
    ''' Anything that needs to get done before the job is dispatched '''
    if job.command in jobcommand_map:
        return jobcommand_map[job.command].preprocess_job(job)
    else:
        return job

def postprocess_job(job, state):
    ''' Anything that needs to get done after the job is completed '''
    if job.command in jobcommand_map:
        return jobcommand_map[job.command].postprocess_job(job, state)
    else:
        logger.debug("Skipping postprocessing because the job's command is not in jobcommand_map.")
        return job

def query_more_jobs(job_cache):
    statuses_to_fetch = (Job.StatusEnum.NEW, Job.StatusEnum.REQUEUE)
    QUERY_LIMIT=50
    for active_jobset in JobSet.objects.filter(active=True).order_by('-priority'):
        jobs = active_jobset.jobs.filter(status_enum__in=statuses_to_fetch).order_by('transaction_id','id')[:QUERY_LIMIT]
        for job in jobs:
            job_cache.add(job)

def generate_jobs(job_cache):
    #statuses_to_fetch = (Job.StatusEnum.NEW, Job.StatusEnum.REQUEUE)
    #QUERY_LIMIT=50
    #prev_jobs = set()
    while True:
        if len(job_cache) > 0:
            yield job_cache.pop()
        else:
            query_more_jobs(job_cache)
            threading.Thread(target=query_more_jobs, args=(job_cache,)).start()
            raise StopIteration
            """
            for active_jobset in JobSet.objects.filter(active=True).order_by('-priority'):
                jobs = active_jobset.jobs.filter(status_enum__in=statuses_to_fetch).order_by('transaction_id','id')[:QUERY_LIMIT]
                for job in jobs:
                    job_cache.append(job)
            set_job_cache = set(job_cache)
            if not job_cache or set_job_cache == prev_jobs:  # The latter condition means all the jobs we've retrieved are unrunnable for some reason.
                if not any(check_readiness(j) for j in job_cache):
                    raise StopIteration
            
            prev_jobs = set_job_cache
            """
    
job_cache = LockingOrderedSet()
job_generator = generate_jobs(job_cache)

def get_next_job(msgbytes):
    global job_generator
    global job_cache
    t0 = datetime.now()
    logger.debug("Looking for the next job.")
    request = protocols.unpack(protobuf.ReaperJobRequest, msgbytes)
    
    i = 0
    for job in job_generator:
        i += 1
        logger.debug("Checking job %d" % i)
        if check_readiness(job):
            logger.debug("Job %d OK." % i)
            break
        else:
            logger.debug("Job %d rejected by JobCommand for %s" % (i, job.command))
    else:
        logger.info("No jobs available. job_generator will be reset.")
        job_generator = generate_jobs(job_cache) # reset the job generator
        logger.info("No jobs available")
        return protocols.pack(protobuf.ReaperJobResponse,{'job_available': False})
    t1 = datetime.now()
    logger.debug("Found usable job in %d iterations (%s)" % ( i, str(t1-t0) ) )
            
    job = preprocess_job(job)
    response = {
        'job_available' : True,
        'uuid' : job.uuid,
        'command' : job.command,
        'args' : json.loads(job.arguments or '[]'),
        }
    logger.info("Sending job %s to reaper %s (%s)" % (job.uuid[:8], request.reaper_uuid[:8], str(datetime.now() - t1)))
    job.status = "dispatched"
    job.processor = request.reaper_uuid
    #dblock.acquire()
    job.save()
    job = None
    #dblock.release()
    if options.show_queries:
        # print the slowest queries
        from django.db import connection
        from pprint import pprint
        pprint([q for q in connection.queries if float(q['time']) > 0.001])
    return protocols.pack(protobuf.ReaperJobResponse, response)
    
####
# Job Status Updates
###

def job_does_not_exist(uuid):
    logger.warning("Got a status update about a nonexistant job.  UUID: %s" % uuid)
    
def reaper_does_not_exist(reaper_uuid):
    # <shrug> Log a warning, register the reaper
   logger.warning("Dispatch received a status message from unregistered reaper %s.  Reaper record will be created." % request['reaper_id'])
   register_reaper(protocols.pack(protobuf.ReaperRegistrationRequest, {'reaper_uuid':reaper_uuid, 'reaper_type':'generic'}))

def verify_reaper_id(job_uuid, reported_uuid, recorded_uuid):
    ''' Warn if a status message comes back from a reaper other than the one we expect for a given job. '''
    try:
        assert reported_uuid == recorded_uuid
    except AssertionError:
        tup = (job_uuid[:8], reported_uuid[:8], recorded_uuid[:8])
        logger.warning("Job %s expected to be handled by Reaper %s, but a status message came from reaper %s.  Probably not good." % tup )

def job_started(msgbytes):
    '''Update the Job record to with properties defined at job start (pid, start_time,...)'''
    request = protocols.unpack(protobuf.ReaperJobStartRequest, msgbytes)
    logger.debug("Received job start message: %s" % str(request))
    try:
        dblock.acquire()
        job = Job.objects.get(uuid=request.job_id)
        logger.debug("Got job %s from DB." % job.uuid[:8])
    except Job.DoesNotExist:
        job_does_not_exist(request.job_id)
        #return protocols.pack(protobuf.AckResponse, {'ack': protobuf.AckResponse.NOACK})
        raise
    verify_reaper_id(request.job_id, request.reaper_id, job.processor)
    
    job.time_started = request.start_time.replace('T',' ') # django DateTimeField should be able to parse it this way. (pyiso8601 would be the alternative).
    job.pid = request.pid
    job.status = request.state or 'processing'

    # get reaper & set current job...
    job.save()
    logger.debug("Job %s saved" % job.uuid[:8])
    try:
        reaper = Reaper.objects.get(uuid=request.reaper_id)
    except Reaper.DoesNotExist:
        # <shrug> Log a warning, register the reaper
        logger.warning("Dispatch received a status message from unregistered reaper %s.  Probably not good." % request['reaper_id'])
        register_reaper(protocols.pack(protobuf.ReaperRegistrationRequest, {'reaper_uuid':request.reaper_id, 'reaper_type':'generic'}))
        reaper = Reaper.objects.get(uuid=request.reaper_id)
    reaper.current_job = job    
    reaper.save()
    logger.debug("Reaper %s saved" % reaper.uuid[:8])
    dblock.release()
    
    # ...
    resp = {'ack': protobuf.AckResponse.ACK}
    logger.debug("Response to send: " + str(resp))
    return protocols.pack(protobuf.AckResponse, resp)

def job_ended(msgbytes):
    '''Update job record with properties defined at job end time ()'''
    request = protocols.unpack(protobuf.ReaperJobEndRequest, msgbytes)
    logger.info("Job %s ended: %s" % (request.job_id[:8], request.state))
    dblock.acquire()
    try:
        job = Job.objects.get(uuid=request.job_id)
    except Job.DoesNotExist:
        job_does_not_exist(request.job_id)
        raise
        #return protocols.pack(protobuf.AckResponse, {'ack': protobuf.AckResponse.NOACK})
        
    job.status = request.state or 'ended'
    job.time_ended = request.end_time.replace('T',' ') # django DateTimeField should be able to parse it this way. (pyiso8601 would be the alternative).
    job.output = request.output
    job = postprocess_job(job, request.state)
    job.save()
    # TODO: get reaper and increment job count
    try:
        reaper = Reaper.objects.get(uuid=job.processor)
        reaper.jobcount += 1
        reaper.current_job_id = None
        reaper.save()
    except Reaper.DoesNotExist:
        # <shrug> Log a warning
        logger.warning("A job ended that was assigned to an unregistered reaper %s.  Probably not good." % request.reaper_id)
        
        
    if request.state == 'complete' and job.creates_new_asset:
        try:
            job.spawn_output_asset()
        except:
            logger.error("ASSET CREATION FAILED FOR JOB %s" % job.uuid)
            sys.excepthook(*sys.exc_info())
            job.status = "asset_creation_fail"
            job.save()
            
    dblock.release()
    
    return protocols.pack(protobuf.AckResponse, {'ack': protobuf.AckResponse.ACK})
    
###
# Handlers
###

def command_handler(msg):
    """ Unpack a message and process commands 
        Speaks the command protocol.
    """
    #cmd = protocols.unpack(protocols.Command, msg.body)
    request = protocols.unpack(protobuf.RpcRequestWrapper, msg.body)
    logger.debug("command_handler got a command: %s" % str(request.method))
    response = dotdict()
    response.sequence_number = request.sequence_number
    
    if request.method in command_map:
        t0 = datetime.now()
        try:
            response.payload = globals()[command_map[request.method]](request.payload)
            response.error = False
        except Exception as e:
            logger.error("Error in command '%s': %s %s" % (request.method, type(e),  str(e.args)))
            # TODO: send a stack trace.
            traceback.print_tb(sys.exc_info()[2]) # traceback
            response.payload = ''
            response.error = True
            response.error_string = str(e)
        t1 = datetime.now()
        logger.info("COMMAND %s finished in %s." % (request.method, str(t1-t0)))
    else:
        logger.error("Invalid Command: %s" % request.method)
        response.payload = None
        response.error = True
        response.error_text = "Invalid Command: %s" % request.method

    mb.basic_ack(msg.delivery_tag)
    response_bytes = protocols.pack(protobuf.RpcResponseWrapper, response)
    mb.basic_publish(Message(response_bytes), routing_key=request.requestor)
        
    
def consume_loop(mb, shutdown_event):
    logger.debug("Starting dispatch consume loop.")
    while mb.channel.callbacks and not shutdown_event.is_set():
        mb.wait()
    logger.debug("dispatch consume loop terminating.")
        

def _shutdown(*args):
    logger.info("Initiating Shutdown")
    mb.shutdown_event.set()
    time.sleep(2)
    sys.exit(1)
    
def shutdown():
    _shutdown()


    
def init():
    logger.debug("dispatch daemon initializing")
    global command_ctag, status_ctag, thread_consume_loop, shutdown_event
    shutdown_event = threading.Event()
    logging.getLogger('messagebus').setLevel(logging.DEBUG)

    if options.requeue_lost_jobs:
        logger.info("Resetting lost jobs.")
        for js in JobSet.objects.filter(active=True):
            js.jobs.filter(status__in=('dispatched','processing')).update(status='requeue')
    
    atexit.register(shutdown)
    
    # setup command queue
    CONTROL_QUEUE = 'control.dispatch'
    logger.debug("Setting up Command listener")
    mb.exchange_declare('Control_Exchange', type='direct')
    mb.queue_declare(CONTROL_QUEUE,auto_delete=True)
    if not options.restart:
        logger.info ("Purging control queue")
        mb.queue_purge(CONTROL_QUEUE)
    mb.queue_bind(queue=CONTROL_QUEUE, exchange='Control_Exchange', routing_key='dispatch')
    command_ctag = mb.basic_consume(callback=command_handler, queue=CONTROL_QUEUE)

    logger.debug("Launching consume thread.")
    mb.start_consuming()


if __name__ == '__main__':
    logger.debug("__name__ == '__main__'")
    global options
    parser = optparse.OptionParser()
    parser.add_option('-r', '--restart', dest="restart", action='store_true', help="Don't purge the control queue.")
    parser.add_option('-d', '--debug', dest='debug', action='store_true', help='Turn on debug logging.')
    parser.add_option('--queries', dest='show_queries', action='store_true', help='Print out the slow queries (django.db.connection.queries)')
    parser.add_option('--lost-jobs', dest='requeue_lost_jobs', action='store_true', help="Requeue jobs marooned with a 'dispatched' or 'processing' status'.")
    (options, args) = parser.parse_args()
    
    if options.debug:
        logger.setLevel(logging.DEBUG)
    logger.debug("Starting init()")
    init()
    try:
        while True:
            if not mb.consumption_thread.is_alive():
                logger.error("Consumtion thread died.  Shutting down dispatch.")
                shutdown()
            time.sleep(0.01)
    except KeyboardInterrupt:
        logger.info("Got a keyboard interrupt.  Shutting down dispatch.")
        shutdown()
        sys.exit(0)

