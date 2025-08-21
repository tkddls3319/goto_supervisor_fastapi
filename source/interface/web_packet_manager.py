import sys
import os
import re
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from pydantic import BaseModel
from typing import List, Dict

class QueryReq(BaseModel):
    query: List[Dict[str, str]]
