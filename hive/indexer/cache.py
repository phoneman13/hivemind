import json
import logging
import math
import collections
import time

from funcy.seqs import first
from hive.db.methods import query
from steem.amount import Amount
from steem import Steem
from steem.utils import parse_time

log = logging.getLogger(__name__)


def get_img_url(url, max_size=1024):
    if url:
        url = url.strip()
    if url and len(url) < max_size and url[0:4] is 'http':
        return url


def score(rshares, created_timestamp, timescale=480000):
    mod_score = rshares / 10000000.0
    order = math.log10(max((abs(mod_score), 1)))
    sign = 1 if mod_score > 0 else -1
    return sign * order + created_timestamp / timescale


# not yet in use. need to get these fields into cache table.
def get_stats(post):
    net_rshares_adj = 0
    neg_rshares = 0
    total_votes = 0
    up_votes = 0
    for v in post['active_votes']:
        if v['percent'] == 0:
            continue

        total_votes += 1
        rshares = int(v['rshares'])
        sign = 1 if v['percent'] > 0 else -1
        if sign > 0:
            up_votes += 1
        if sign < 0:
            neg_rshares += rshares

            # For graying: sum up total rshares, but ignore neg rep users and tiny downvotes
        if str(v['reputation'])[0] != '-' and not (sign < 0 and len(str(rshares)) < 11):
            net_rshares_adj += rshares

    # take negative rshares, divide by 2, truncate 10 digits (plus neg sign), count digits.
    # creates a cheap log10, stake-based flag weight. 1 = approx $400 of downvoting stake; 2 = $4,000; etc
    flag_weight = max((len(str(neg_rshares / 2)) - 11, 0))

    allow_delete = post['children'] == 0 and int(post['net_rshares']) <= 0
    has_pending_payout = Amount(post['pending_payout_value']).amount >= 0.02
    author_rep = rep_log10(post['author_reputation'])

    gray_threshold = -9999999999
    low_value_post = net_rshares_adj < gray_threshold and author_rep < 65

    gray = not has_pending_payout and (author_rep < 1 or low_value_post)
    hide = not has_pending_payout and (author_rep < 0)

    return {
        'hide': hide,
        'gray': gray,
        'allow_delete': allow_delete,
        'author_rep': author_rep,
        'flag_weight': flag_weight,
        'total_votes': total_votes,
        'up_votes': up_votes
    }


def batch_queries(queries):
    query("START TRANSACTION")
    for (sql, params) in queries:
        query(sql, **params)
    query("COMMIT")


# TODO: escape strings for mysql
def escape(str):
    return str

# calculate Steemit rep score
def rep_log10(rep):
    def log10(str):
        leading_digits = int(str[0:4])
        log = math.log10(leading_digits) + 0.00000001
        n = len(str) - 1
        return n + (log - int(log))

    rep = str(rep)
    if rep == "0":
        return 25

    sign = -1 if rep[0] == '-' else 1
    if sign < 0:
        rep = rep[1:]

    out = log10(rep)
    out = max(out - 9, 0) * sign  # @ -9, $1 earned is approx magnitude 1
    out = (out * 9) + 25          # 9 points per magnitude. center at 25
    return round(out, 2)


def vote_csv_row(vote):
    return ','.join((vote['voter'], str(vote['rshares']), str(vote['percent']), str(rep_log10(vote['reputation']))))


def generate_cached_post_sql(id, post, updated_at):
    md = None
    try:
        md = json.loads(post['json_metadata'])
        if type(md) is not dict:
            md = {}
    except json.decoder.JSONDecodeError:
        pass

    thumb_url = ''
    if md and 'image' in md:
        thumb_url = get_img_url(first(md['image'])) or ''
        md['image'] = [thumb_url]

    # clean up tags, check if nsfw
    tags = [post['category']]
    if md and 'tags' in md and type(md['tags']) == list:
        tags = tags + md['tags']
    tags = set(map(lambda str: (str or '').lower(), tags))
    is_nsfw = int('nsfw' in tags)

    # payout date is last_payout if paid, and cashout_time if pending.
    payout_at = post['last_payout'] if post['cashout_time'][0:4] == '1969' else post['cashout_time']

    # get total rshares, and create comma-separated vote data blob
    rshares = sum(int(v['rshares']) for v in post['active_votes'])
    csvotes = "\n".join(map(vote_csv_row, post['active_votes']))

    # these are rshares which are PENDING
    payout_declined = False
    if Amount(post['max_accepted_payout']).amount == 0:
        payout_declined = True
    elif len(post['beneficiaries']) == 1:
        benny = first(post['beneficiaries'])
        if benny['account'] == 'null' and int(benny['weight']) == 10000:
            payout_declined = True

    # total payout (completed and/or pending)
    payout = sum([
        Amount(post['total_payout_value']).amount,
        Amount(post['curator_payout_value']).amount,
        Amount(post['pending_payout_value']).amount,
    ])

    # total promotion cost
    promoted = Amount(post['promoted']).amount

    # trending scores
    timestamp = parse_time(post['created']).timestamp()
    hot_score = score(rshares, timestamp, 10000)
    trend_score = score(rshares, timestamp, 480000)

    # TODO: evaluate adding these columns. Some CAN be computed upon access.
    #   Some need to be in the db if queries will depend on them. (is_hidden)
    # is_no_payout
    # is_full_power
    # is_hidden
    # is_grayed
    # flag_weight
    # total_votes
    # up_votes

    values = collections.OrderedDict([
        ('post_id', '%d' % id),
        ('title', "%s" % escape(post['title'])),
        ('preview', "%s" % escape(post['body'][0:1024])),
        ('img_url', "%s" % escape(thumb_url)),
        ('payout', "%f" % payout),
        ('promoted', "%f" % promoted),
        ('payout_at', "%s" % payout_at),
        ('updated_at', "%s" % updated_at),
        ('created_at', "%s" % post['created']),
        ('children', "%d" % post['children']), # TODO: remove this field
        ('rshares', "%d" % rshares),
        ('votes', "%s" % escape(csvotes)),
        ('json', "%s" % escape(json.dumps(md))),
        ('is_nsfw', "%d" % is_nsfw),
        ('sc_trend', "%f" % trend_score),
        ('sc_hot', "%f" % hot_score)
    ])
    fields = values.keys()

    cols   = ', '.join( fields )
    params = ', '.join( [':'+k for k in fields] )
    update = ', '.join( [k+" = :"+k for k in fields][1:] )
    sql = "INSERT INTO hive_posts_cache (%s) VALUES (%s) ON DUPLICATE KEY UPDATE %s"
    return (sql % (cols, params, update), values)

def cache_missing_posts():
    sql = "SELECT id, author, permlink FROM hive_posts WHERE is_deleted = 0 AND id > (SELECT IFNULL(MAX(post_id),0) FROM hive_posts_cache) ORDER BY id"
    rows = list(query(sql))
    update_posts_batch(rows)

def update_posts_batch(tuples):
    steemd = Steem().steemd
    buffer = []
    updated_at = steemd.get_dynamic_global_properties()['time']

    processed = 0
    start_time = time.time()
    for (id, author, permlink) in tuples:
        post = steemd.get_content(author, permlink)
        sql = generate_cached_post_sql(id, post, updated_at)
        buffer.append(sql)

        if len(buffer) == 250:
            batch_queries(buffer)
            processed += len(buffer)
            rem = len(tuples) - processed
            rate = processed / (time.time() - start_time)
            print("{} of {} ({}/s) {}m remaining".format(
                processed, len(tuples), round(rate, 1),
                round((len(tuples) - processed) / rate / 60, 2) ))
            buffer = []

    batch_queries(buffer)

# testing
# -------
def run():
    cache_missing_posts()
    #post = Steemd().get_content('roadscape', 'script-check')
    #print(generate_cached_post_sql(1, post, '1970-01-01T00:00:00'))


if __name__ == '__main__':
    # setup()
    run()
