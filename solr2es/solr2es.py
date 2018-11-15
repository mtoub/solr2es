#!/usr/bin/env python
import asyncio
import getopt
import itertools
import logging
import sys
from json import loads, dumps

import aiohttp
import asyncio_redis
import redis
from elasticsearch import Elasticsearch
from elasticsearch_async import AsyncElasticsearch
from pysolr import Solr

logging.basicConfig(format='%(asctime)s [%(name)s] %(levelname)s: %(message)s')
LOGGER = logging.getLogger('solr2es')
LOGGER.setLevel(logging.INFO)

DEFAULT_ES_DOC_TYPE = 'doc'


class Solr2Es(object):
    def __init__(self, solr, es, refresh=False) -> None:
        super().__init__()
        self.solr = solr
        self.es = es
        self.refresh = refresh

    def migrate(self, index_name, mapping=None) -> int:
        nb_results = 0
        if not self.es.indices.exists([index_name]):
            self.es.indices.create(index_name, body=mapping)
        for results in self.produce_results():
            actions = create_es_actions(index_name, results)
            response = self.es.bulk(actions, index_name, DEFAULT_ES_DOC_TYPE, refresh=self.refresh)
            nb_results += len(results)
            if response['errors']:
                for err in response['items']:
                    LOGGER.warning(err)
                nb_results -= len(response['items'])
        LOGGER.info('processed %s documents', nb_results)
        return nb_results

    def produce_results(self):
        nb_results = 0
        nb_total = 0
        cursor_ended = False
        kwargs = dict(cursorMark='*', sort='id asc')
        while not cursor_ended:
            results = self.solr.search('*:*', **kwargs)
            if kwargs['cursorMark'] == '*':
                nb_total = results.hits
                LOGGER.info('found %s documents', nb_total)
            if kwargs['cursorMark'] != results.nextCursorMark:
                kwargs['cursorMark'] = results.nextCursorMark
                nb_results += len(results)
                if nb_results % 10000 == 0:
                    LOGGER.info('read %s docs of %s (%s %% done)', nb_results, nb_total, (100 * nb_results)/nb_total)
                yield results
            else:
                cursor_ended = True


class Solr2EsAsync(object):
    def __init__(self, aiohttp_session, aes, solr_url, refresh=False) -> None:
        super().__init__()
        self.solr_url = solr_url
        self.aiohttp_session = aiohttp_session
        self.aes = aes
        self.refresh = refresh

    async def migrate(self, index_name) -> int:
        nb_results = 0
        async for results in self.produce_results():
            actions = create_es_actions(index_name, results)
            await self.aes.bulk(actions, index_name, DEFAULT_ES_DOC_TYPE, refresh=self.refresh)
            nb_results += len(results)
        return nb_results

    async def produce_results(self):
        cursor_ended = False
        nb_results = 0
        nb_total = 0
        kwargs = dict(cursorMark='*', sort='id asc', q='*:*', wt='json')
        while not cursor_ended:
            async with self.aiohttp_session.get(self.solr_url + '/select/', params=kwargs) as resp:
                json = loads(await resp.text())
                if kwargs['cursorMark'] == '*':
                    nb_total = int(json['response']['numFound'])
                    LOGGER.info('found %s documents', json['response']['numFound'])
                if kwargs['cursorMark'] != json['nextCursorMark']:
                    kwargs['cursorMark'] = json['nextCursorMark']
                    nb_results += len(json['response']['docs'])
                    if nb_results % 10000 == 0:
                        LOGGER.info('read %s docs of %s (%s %% done)', nb_results, nb_total,
                                    (100 * nb_results) / nb_total)
                    yield json['response']['docs']
                else:
                    cursor_ended = True
        LOGGER.info('processed %s documents', nb_results)


class RedisConsumer(object):
    def __init__(self, redis) -> None:
        self.redis = redis

    def consume(self, producer):
        for results in producer():
            self.redis.lpush('solr2es:queue', *map(dumps, results))


class RedisConsumerAsync(object):
    def __init__(self, redis) -> None:
        self.redis = redis

    async def consume(self, producer):
        async for results in producer():
            await self.redis.lpush('solr2es:queue', list(map(dumps, results)))


def create_es_actions(index_name, solr_results):
    results_ = [({'index': {'_index': index_name, '_type': DEFAULT_ES_DOC_TYPE, '_id': row['id']}}, remove_arrays(row))
                for row in solr_results]
    return '\n'.join(list(map(lambda d: dumps(d), itertools.chain(*results_))))


def remove_arrays(row):
    def filter(value):
        if type(value) is list:
            return value[0]
        else:
            return value
    return {k: filter(v) for k, v in row.items()}


def dump_into_redis(solrurl, redishost):
    LOGGER.info('dump from solr (%s) into redis (host=%s)', solrurl, redishost)
    RedisConsumer(redis.Redis(host=redishost)).consume(Solr2Es(Solr(solrurl, always_commit=True), None).produce_results)


def resume_from_redis(redishost, esurl, name):
    LOGGER.info('resume from redis (host=%s) to elasticsearch (%s) index %s', redishost, esurl, name)


def migrate(solrurl, esurl, name):
    LOGGER.info('migrate from solr (%s) into elasticsearch (%s) index %s', solrurl, esurl, name)
    Solr2Es(Solr(solrurl, always_commit=True), Elasticsearch(host=esurl)).migrate(name)


async def aiodump_into_redis(solrurl, redishost):
    LOGGER.info('asyncio dump from solr (%s) into redis (host=%s)', solrurl, redishost)
    async with aiohttp.ClientSession() as session:
        await RedisConsumerAsync(await asyncio_redis.Pool.create(host=redishost, port=6379, poolsize=10)).\
            consume(Solr2EsAsync(session, None, solrurl).produce_results)


async def aioresume_from_redis(redishost, esurl, name):
    LOGGER.info('asyncio resume from redis (host=%s) to elasticsearch (%s) index %s', redishost, esurl, name)


async def aiomigrate(solrurl, esurl, name):
    LOGGER.info('asyncio migrate from solr (%s) into elasticsearch (%s) index %s', solrurl, esurl, name)
    async with aiohttp.ClientSession() as session:
        await Solr2EsAsync(session, AsyncElasticsearch(hosts=[esurl]), solrurl).migrate(name)


def usage(argv):
    print('Usage: %s action' % argv[0])
    print('\t-m|--migrate: migrate solr to elasticsearch')
    print('\t-r|--resume: resume from redis')
    print('\t-d|--dump: dump into redis')
    print('\t-a|--async: use python 3 asyncio')
    print('\t--solrurl: url solr (default http://solr:8983/solr/my_core)')
    print('\t--index: index name (default solr2es)')
    print('\t--esurl: elasticsearch url (default elasticsearch:9200)')
    print('\t--redishost: redis host (default redis)')


if __name__ == '__main__':
    options, remainder = getopt.gnu_getopt(sys.argv[1:], 'hmdra', ['help', 'migrate', 'dump', 'resume', 'async', 'solrurl=', 'esurl=', 'redishost=', 'index='])
    if len(sys.argv) == 1:
        usage(sys.argv)
        sys.exit()

    aioloop = asyncio.get_event_loop()
    with_asyncio = False
    solrurl = 'http://solr:8983/solr/my_core'
    esurl = 'elasticsearch:9200'
    redishost = 'redis'
    index_name = 'solr2es'
    action = 'migrate'
    for opt, arg in options:
        if opt in ('-h', '--help'):
            usage(sys.argv)
            sys.exit()

        if opt in ('-a', '--async'):
            with_asyncio = True

        if opt == '--solrurl':
            solrurl = arg

        if opt == '--redishost':
            redishost = arg

        if opt == '--esurl':
            esurl = arg

        if opt == '--index':
            index_name = arg

        if opt in ('-d', '--dump'):
            action = 'dump'
        elif opt in ('-r', '--resume'):
            action = 'resume'
        elif opt in ('-m', '--migrate'):
            action = 'migrate'

    if action == 'migrate':
        aioloop.run_until_complete(aiomigrate(solrurl, esurl, index_name)) if with_asyncio else migrate(solrurl, esurl, index_name)
    elif action == 'dump':
        aioloop.run_until_complete(aiodump_into_redis(solrurl, redishost)) if with_asyncio else dump_into_redis(solrurl, redishost)
    elif action == 'resume':
        aioloop.run_until_complete(aioresume_from_redis(redishost, esurl, index_name)) if with_asyncio else resume_from_redis(redishost, esurl, index_name)
