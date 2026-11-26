"""Exercise representative JavaScript and TypeScript copy routes without running their code."""

from importlib.util import find_spec
import unittest

from setrya.discovery import copy_sites, typescript


@unittest.skipUnless(
    all(find_spec(name) for name in ("tree_sitter", "tree_sitter_typescript", "tree_sitter_javascript")),
    "install setrya[typescript]",
)
class NodeCopyPatternsTests(unittest.TestCase):
    def setUp(self):
        self.parsers = typescript.load_parser()

    def scan(self, source, *, path="routes.ts"):
        unknown = []
        found = typescript.inspect(source.encode(), path, self.parsers, copy_sites.CopySite, copy_sites._digest,
                                   512, report_unknown=lambda path, reason: unknown.append((path, reason)))
        return [(row.operation, row.writer) for row in found], [reason for _, reason in unknown]

    def test_javascript_commonjs_and_shadowed_require(self):
        cases = {
            "routes.js": "const { createClient: connect } = require('redis'); connect().set('k', value);",
            "routes.mjs": "import { createClient } from 'redis'; const client = createClient(); client.set('k', val);",
            "routes.cjs": "const open = require('redis').createClient; open().set('k', value);",
            "routes.jsx": "import {createClient} from 'redis'; const cache = createClient(); "
                          "const ui = <button onClick={() => cache.set('k', val)}>Click</button>;",
        }
        for path, source in cases.items():
            with self.subTest(path=path):
                self.assertTrue(typescript.is_source(path))
                self.assertEqual(self.scan(source, path=path), ([("redis.set", None)], []))
        shadowed = """
            function demo(require, value) {
                const cache = require('redis').createClient();
                cache.set('k', value);
            }
        """
        self.assertEqual(self.scan(shadowed, path="example.cjs"), ([], []))

    def test_sql_drivers_and_object_relational_mappers(self):
        source = """
            import pg from "pg";
            import postgres from "postgres";
            import { PrismaClient } from "@prisma/client";
            import { drizzle } from "drizzle-orm/node-postgres";
            import { Kysely } from "kysely";
            const pool = new pg.Pool();
            const sql = postgres();
            const prisma = new PrismaClient();
            const db = drizzle(pool);
            const builder = new Kysely();
            await pool.query({text: 'INSERT INTO notes (body) VALUES ($1)', values: [answer]});
            const client = await pool.connect();
            const query = "UPDATE notes SET body=$1";
            await client.query(query, [answer]);
            await sql ` INSERT INTO notes (body) VALUES (${answer}) `;
            await prisma.answer.create({ data: { body: answer } });
            await prisma.$executeRaw ` UPDATE notes SET body=${answer} `;
            await db.insert(notes).values({body: answer});
            await db.update(notes).set({body: answer});
            await builder.insertInto("notes").values({body: answer}).execute();
            await builder.updateTable("notes").set({body: answer}).execute();
            await sql ` SELECT * FROM notes WHERE body=${answer} `;
            await pool.query("SELECT body FROM notes;");
            await builder.selectFrom("notes").selectAll().execute();
        """
        sites, unknown = self.scan(source)
        self.assertEqual([row[0] for row in sites], [
            "pg.query", "pg.query", "postgres", "prisma.model.create", "prisma.$executeRaw",
            "drizzle.insert", "drizzle.update", "kysely.insertInto", "kysely.updateTable",
        ])
        self.assertEqual(unknown, [])

    def test_ambiguous_storage_calls_are_coverage_unknown_even_alongside_a_known_write(self):
        source = """
            import pg from 'pg';
            import { PrismaClient } from '@prisma/client';
            const pool = new pg.Pool();
            await pool.query('INSERT INTO notes VALUES ($1)', [data]);
            await pool.query(statement);
            const db = new PrismaClient();
            db.$transaction(useDb);
        """
        sites, unknown = self.scan(source)
        self.assertEqual(sites, [("pg.query", None)])
        self.assertCountEqual(unknown, [typescript.UNRECOGNIZED_STORAGE, typescript.UNRESOLVED_WRITE])
        self.assertEqual(self.scan("require('pg');", path="load.cjs"), ([], [typescript.UNRECOGNIZED_STORAGE]))

    def test_node_globals_and_local_callback_worker_boundaries(self):
        source = """
            const { createClient } = require('redis');
            const cache = createClient();
            function save(client) { client.set("next", data); }
            function wrap() { cache.set("time", data); }
            setTimeout(wrap, 10);
            setImmediate(() => cache.set("immediate", data));
            queueMicrotask(() => cache.set("microtask", data));
            process.nextTick(save, cache);
            globalThis.setTimeout(() => cache.set('explicit-global', data), 1);
            function masked(setTimeout, process, globalThis) {
              setTimeout(() => cache.set('shadowed-timer', data), 10);
              process.nextTick(() => cache.set('shadowed-process', data));
              globalThis.queueMicrotask(() => cache.set('shadowed-global', data));
            }
        """
        sites, unknown = self.scan(source, path="routes.cjs")
        self.assertEqual([writer for _, writer in sites], ["timer"] * 5 + [None] * 3)
        self.assertEqual(unknown, [])

    def test_class_fields_type_only_annotations_and_local_injection(self):
        source = """
            import { createClient } from "redis";
            import type { RedisClientType as Client } from "redis";
            class Answers {
              #own = createClient();
              readonly explicit: Client;
              constructor(private readonly shared: Client) { this.explicit = shared; }
              save() {
                this.#own.set("own", answer);
                this.explicit.set("explicit", answer);
                this.shared.set("shared", answer);
              }
            }
            function typed(cache: Client) { cache.set("typed", answer); }
            const cache = createClient();
            function injected(cache) { cache.set("injected", answer); }
            injected(cache);
            function shadow<Client>(cache: Client) { cache.set("type-shadowed", answer); }
            function mixed(cache) { cache.set("ambiguous", answer); }
            mixed(cache);
            mixed(other);
            class Reassigned {
              r = createClient();
              setAlternate() { this.r = other; }
              save() { this.r.set("reassigned", answer); }
            }
        """
        self.assertEqual(self.scan(source), ([("redis.set", None)] * 5, []))

    def test_adapters_for_agent_memory_vector_files_and_state(self):
        source = """
            import { generateText, streamText } from "ai";
            import { openai } from "@ai-sdk/openai";
            import { Memory as Mastra } from "@mastra/memory";
            import { StateGraph, MemorySaver } from "@langchain/langgraph";
            import { Memory } from "mem0ai/oss";
            import Anthropic from "@anthropic-ai/sdk";
            import { query } from "@anthropic-ai/claude-agent-sdk";
            import Letta from "@letta-ai/letta-client";
            import { ChromaClient } from "chromadb";
            import { createClient } from "@supabase/supabase-js";
            import { Index } from "@upstash/vector";
            await generateText({ model: openai.responses(model), prompt });
            await streamText({ model: openai.responses(model), prompt, providerOptions: { openai: { store: false } }});
            await streamText({ model: openai.chat(model), prompt });
            const mastra = new Mastra();
            await mastra.saveMessages({ messages });
            const saver = new MemorySaver();
            await saver.put(config, checkpoint, metadata);
            const graph = new StateGraph(state).compile({ checkpointer: saver });
            await graph.invoke(input);
            const memory = new Memory();
            await memory.add(messages, { userId });
            const claude = new Anthropic();
            await claude.beta.files.upload({ file });
            query({ prompt, options: { persistSession: false } });
            query({ prompt });
            const letta = new Letta();
            await letta.agents.messages.create(id, { messages });
            const chroma = new ChromaClient();
            const collection = await chroma.getOrCreateCollection({ name });
            await collection.upsert({ documents, ids });
            const supabase = createClient(url, key);
            await supabase.from("notes").upsert({ body: data });
            await supabase.storage.from("assets").upload(path, file);
            const index = new Index();
            await index.upsert({ id, vector });
        """
        sites, unknown = self.scan(source)
        self.assertEqual([row[0] for row in sites], [
            "ai-sdk.generateText", "mastra.saveMessages", "langgraph-checkpointer.put",
            "langgraph-persistent.invoke", "mem0.add", "anthropic.beta.files.upload", "claude-agent.query",
            "letta.agents.messages.create", "chroma-collection.upsert", "supabase-query.upsert",
            "supabase-bucket.upload", "upstash-vector.upsert",
        ])
        self.assertEqual(unknown, [])
