import os

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://museum:museum_pass@localhost:17768/museum_case')
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:16368/0')
JWT_SECRET = os.getenv('JWT_SECRET', 'museum-case-anomaly-secret')
JWT_ALGORITHM = 'HS256'
