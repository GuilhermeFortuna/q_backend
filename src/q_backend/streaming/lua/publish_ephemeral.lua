-- src/q_backend/streaming/lua/publish_ephemeral.lua
-- KEYS: seq_key, topic_epoch_key, stream_key, latest_key
-- ARGV: candidate_epoch, header_json_without_seq_epoch, payload, maxlen, routing_key_string
-- SET epoch NX; INCR seq (if epoch was just created, seq was absent -> 1);
-- header gains seq+epoch; XADD MAXLEN ~ maxlen; HSET latest routing_key_string -> id
-- returns {epoch, seq, id}

local seq_key = KEYS[1]
local epoch_key = KEYS[2]
local stream_key = KEYS[3]
local latest_key = KEYS[4]

local candidate_epoch = ARGV[1]
local header_json_in = ARGV[2]
local payload = ARGV[3]
local maxlen = tonumber(ARGV[4])
local routing_key_string = ARGV[5]

local epoch_created = redis.call('SET', epoch_key, candidate_epoch, 'NX')
if epoch_created then
    redis.call('DEL', seq_key)
end

local seq = redis.call('INCR', seq_key)
local epoch = redis.call('GET', epoch_key)

local header = cjson.decode(header_json_in)
header['seq'] = seq
header['epoch'] = epoch
local header_json = cjson.encode(header)

local id
if maxlen and maxlen > 0 then
    id = redis.call('XADD', stream_key, 'MAXLEN', '~', maxlen, '*', 'h', header_json, 'p', payload)
else
    id = redis.call('XADD', stream_key, '*', 'h', header_json, 'p', payload)
end

if routing_key_string and #routing_key_string > 0 then
    redis.call('HSET', latest_key, routing_key_string, id)
end

return {epoch, seq, id}
