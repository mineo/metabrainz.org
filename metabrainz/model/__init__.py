from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# All models must be imported there:
from .user import User
from .token import Token
from .tier import Tier
from .donation import Donation
