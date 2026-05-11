import pymysql
import pymysql.cursors
from config import MEET_DB, WK_DB


def get_meet_conn() -> pymysql.Connection:
    return pymysql.connect(
        **MEET_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_wrike_conn() -> pymysql.Connection:
    return pymysql.connect(
        **WK_DB,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
