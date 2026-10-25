"""Hệ CSDL — a creator's own business database as a data source (DS-02 / ADR-0059 §2).

`src.datasources.dialects` is everything that talks to such a database;
`src.datasources.sources` owns the `database_sources` row and its DSN in the
secret store. Nothing here runs at agent run time yet — that is `data_query`
(DS-04).
"""
