import time
import json

from gevent import sleep
from . import QueueStatReporter
from ..lib import loop
from ..stratum_server import StratumClient


# Parameters: {"current block"'s key name,
#              current timestamp,
#              new key name for "current block" (something like unproc_block_{block_hash}}
solve_rotate_multichain = """
-- Get all the keys so we can find all the sharechains that contributed
local keys = redis.call('HKEYS', ARGV[1])
-- Set the end time of block solve. This also serves to guarentee the key is there...
redis.call('HSET', ARGV[1], 'solve_time', ARGV[2])
-- Rename to new home
redis.call('rename', ARGV[1], ARGV[3])
-- Initialize the new block key with a start time
redis.call('HSET', ARGV[1], 'start_time', ARGV[2])

-- Parse out and rotate all share chains. I'm sure this is terrible, no LUA skillz
local idx_map = {}
for key, val in pairs(keys) do
    local t = {}
    local i = 0
    for w in string.gmatch(val, "%w+") do
        t[i] = w
        i = i + 1
     end
     if t[0] == "chain" and t[2] == "shares" then
         local base = "chain_" .. t[1] .. "_slice"
         local idx = redis.call('incr', base .. "_index")
         redis.pcall('renamenx', base, base .. "_" .. idx)
         table.insert(idx_map, t[1] .. ":" .. idx)
     end
end
return idx_map
"""


class RedisReporter(QueueStatReporter):
    one_sec_stats = ['queued']
    gl_methods = ['_queue_proc', '_report_one_min']
    defaults = QueueStatReporter.defaults.copy()
    defaults.update(dict(redis={}, chain=1))

    def __init__(self, config):
        self._configure(config)
        super(RedisReporter, self).__init__()
        # Import reporter type specific modules here as to not require them
        # for using powerpool with other reporters
        import redis
        self.redis = redis
        # A list of exceptions that would indicate that retrying a queue item
        # COULD EVENTUALLY work (ie, bad connection because server
        # maintenince).  Errors that are likely to occur because of bad
        # coding/edge cases should be let through and data discarded after a
        # few attempts.
        self.queue_exceptions = (redis.exceptions.ConnectionError,
                                 redis.exceptions.InvalidResponse,
                                 redis.exceptions.TimeoutError,
                                 redis.exceptions.ConnectionError,
                                 redis.exceptions.BusyLoadingError)
        self.redis = redis.Redis(**self.config['redis'])
        self.solve_cmd = self.redis.register_script(solve_rotate_multichain)

    @loop(setup='_start_queue')
    def _queue_proc(self):
        name, args, kwargs = self.queue.peek()
        self.logger.debug("Queue running {} with args '{}' kwargs '{}'"
                          .format(name, args, kwargs))
        try:
            func = getattr(self, name, None)
            if func is None:
                raise NotImplementedError(
                    "Item {} has been enqueued that has no valid function!"
                    .format(name))
            func(*args, **kwargs)
        except self.queue_exceptions as e:
            self.logger.error("Unable to process queue item, retrying! "
                              "{} Name: {}; Args: {}; Kwargs: {};"
                              .format(e, name, args, kwargs), exc_info=True)
            sleep(1)
            return False  # Don't do regular loop sleep
        except self.redis.exceptions.ResponseError as e:
            # https://github.com/antirez/redis/blob/b892ea70ae4c2da7a0736943a4ee1915edda838d/src/redis.c#L1291
            error_code = str(e).split(' ')[0]
            if error_code in ['MISCONF', 'NOAUTH', 'OOM', 'NOREPLICAS', 'BUSY']:
                self.logger.error("Unable to process queue item, retrying! "
                                  "{} Name: {}; Args: {}; Kwargs: {};"
                                  .format(e, name, args, kwargs), exc_info=True)
                sleep(1)
                return False  # Don't do regular loop sleep
        except Exception:
            # Log any unexpected problem, but don't retry because we might
            # end up endlessly retrying with same failure
            self.logger.error("Unkown error, queue data discarded!"
                              "Name: {}; Args: {}; Kwargs: {};"
                              .format(name, args, kwargs), exc_info=True)
        # By default we want to remove the item from the queue
        self.queue.get()

    @property
    def status(self):
        return dict(queue_size=self.queue.qsize())

    def _queue_log_one_minute(self, address, worker, algo, stamp, typ, amount):
        # Include worker info if defined
        address += "." + worker
        self.redis.hincrbyfloat(
            "min_{}_{}_{}".format(StratumClient.share_type_strings[typ], algo, stamp),
            address, amount)

    def _queue_add_block(self, address, height, total_subsidy, fees, hex_bits,
                         hex_hash, currency, algo, merged=False, worker=None,
                         **kwargs):
        block_key = 'current_block_{}_{}'.format(currency, algo)
        new_block_key = "unproc_block_{}".format(hex_hash)

        chain_indexes_serial = self.solve_cmd(keys=[], args=[block_key, time.time(), new_block_key])
        chain_indexs = {}
        for chain in chain_indexes_serial:
            chain_id, last_index = chain.split(":")
            chain_indexs["chain_{}_solve_index".format(chain_id)] = last_index
        self.redis.hmset(new_block_key, dict(address=address,
                                             worker=worker,
                                             height=height,
                                             total_subsidy=total_subsidy,
                                             fees=fees,
                                             hex_bits=hex_bits,
                                             hash=hex_hash,
                                             currency=currency,
                                             algo=algo,
                                             merged=int(bool(merged)),
                                             **chain_indexs))

    def _queue_log_share(self, address, shares, algo, currency, merged=False):
        block_key = 'current_block_{}_{}'.format(currency, algo)
        chain_key = 'chain_{}_shares'.format(self.config['chain'])
        chain_slice = 'chain_{}_slice'.format(self.config['chain'])
        user_shares = '{}:{}'.format(address, shares)
        self.redis.hincrbyfloat(block_key, chain_key, shares)
        self.redis.rpush(chain_slice, user_shares)

    def log_share(self, client, diff, typ, params, job=None, header_hash=None, header=None):
        super(RedisReporter, self).log_share(
            client, diff, typ, params, job=job, header_hash=header_hash, header=header)

        if typ != StratumClient.VALID_SHARE:
            return

        for currency in job.merged_data:
            self.queue.put(("_queue_log_share", [], dict(address=client.address,
                                                         shares=diff,
                                                         algo=job.algo,
                                                         currency=currency,
                                                         merged=True)))
        self.queue.put(("_queue_log_share", [], dict(address=client.address,
                                                     shares=diff,
                                                     algo=job.algo,
                                                     currency=job.currency,
                                                     merged=False)))

    def _queue_agent_send(self, address, worker, typ, data, stamp):
        if typ == "hashrate" or typ == "temp":
            stamp = (stamp // 60) * 60
            for did, val in enumerate(data):
                self.redis.hset("{}_{}".format(typ, stamp),
                                "{}_{}_{}".format(address, worker, did),
                                val)
        elif typ == "status":
            self.redis.set("status_{}_{}".format(address, worker), json.dumps(data))
        else:
            self.logger.warn("Recieved unsupported ppagent type {}"
                             .format(typ))

    def agent_send(self, *args, **kwargs):
        self.queue.put(("_queue_agent_send", args, kwargs))
