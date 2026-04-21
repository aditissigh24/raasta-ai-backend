-- Atomic context update for wingman session cache.
-- Executed by Redis as a single atomic operation (no race conditions).
--
-- KEYS[1] : wingman:ctx:{conversation_id}
-- ARGV[1] : JSON string of fields to merge into the stored context
-- ARGV[2] : TTL in seconds (string)
--
-- Returns 1 on success, nil if the key does not exist (evicted).

local key = KEYS[1]
local updates_json = ARGV[1]
local ttl = tonumber(ARGV[2])

local raw = redis.call('GET', key)
if not raw then
    return nil  -- key evicted — caller must re-prime
end

local ctx = cjson.decode(raw)
local updates = cjson.decode(updates_json)

for k, v in pairs(updates) do
    ctx[k] = v
end

redis.call('SET', key, cjson.encode(ctx), 'EX', ttl)
return 1
