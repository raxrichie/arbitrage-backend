import redis

def get_client():
    return redis.Redis(host="localhost", port=6379, db=0)
