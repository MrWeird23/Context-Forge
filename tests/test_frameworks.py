import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest

from contextforge import intelligence
from contextforge.analyzers import SourceFile
from contextforge.frameworks import (
    DjangoAnalyzer,
    FastAPIAnalyzer,
    FlaskAnalyzer,
    FrameworkAnalyzer,
    SQLAlchemyAnalyzer,
    extract_framework_entities,
)
from contextforge.intelligence import (
    build_index,
    find_framework_entities,
    find_relationships,
    trace_symbol,
)
from contextforge.scanner import CODE_EXTENSIONS


def test_fastapi_get_decorator_extracts_evidence_backed_route():
    source = SourceFile(
        "api/users.py",
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "async def list_users():\n"
        "    return []\n",
    )
    analyzer = FastAPIAnalyzer()

    assert isinstance(analyzer, FrameworkAnalyzer)

    entities = analyzer.extract_entities(source)

    assert [
        (
            entity.framework,
            entity.kind,
            entity.name,
            entity.target,
            entity.path,
            entity.line,
            entity.end_line,
            entity.confidence,
            dict(entity.attributes),
        )
        for entity in entities
    ] == [
        (
            "fastapi",
            "route",
            "GET /users",
            "list_users",
            "api/users.py",
            4,
            6,
            "high",
            {"method": "GET", "route": "/users"},
        )
    ]


def test_flask_route_and_shorthand_decorators_extract_declared_methods():
    source = SourceFile(
        "web.py",
        "from flask import Flask as WebApp\n"
        "app = WebApp(__name__)\n"
        "@app.route('/users', methods=['POST', 'GET', 'POST'])\n"
        "def users(): return []\n"
        "@app.route('/default')\n"
        "def default(): return None\n"
        "@app.get('/health')\n"
        "def health(): return None\n",
    )

    entities = FlaskAnalyzer().extract_entities(source)

    assert [
        (
            entity.framework,
            entity.name,
            entity.target,
            entity.line,
            entity.confidence,
            dict(entity.attributes),
        )
        for entity in entities
    ] == [
        (
            "flask",
            "POST /users",
            "users",
            3,
            "high",
            {"method": "POST", "route": "/users"},
        ),
        (
            "flask",
            "GET /users",
            "users",
            3,
            "high",
            {"method": "GET", "route": "/users"},
        ),
        (
            "flask",
            "GET /default",
            "default",
            5,
            "high",
            {"method": "GET", "route": "/default"},
        ),
        (
            "flask",
            "GET /health",
            "health",
            7,
            "high",
            {"method": "GET", "route": "/health"},
        ),
    ]


def test_flask_route_composition_is_conservative_and_evidence_weighted():
    source = SourceFile(
        "web.py",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.get('/api' + '/users')\n"
        "def users(): return []\n"
        "prefix = '/api'\n"
        "@app.get(prefix + '/dynamic')\n"
        "def dynamic(): return []\n"
        "app = object()\n"
        "@app.get('/stale')\n"
        "def stale(): return []\n",
    )

    entities = FlaskAnalyzer().extract_entities(source)

    assert [(entity.name, entity.target, entity.confidence) for entity in entities] == [
        ("GET /api/users", "users", "medium")
    ]


def test_flask_requires_valid_application_and_literal_methods():
    source = SourceFile(
        "web.py",
        "from flask import Flask\n"
        "app = Flask()\n"
        "@app.get('/invalid')\n"
        "def invalid(): return None\n"
        "app = Flask(__name__)\n"
        "methods = ['POST']\n"
        "@app.route('/dynamic', methods=methods)\n"
        "def dynamic(): return None\n",
    )

    assert FlaskAnalyzer().extract_entities(source) == []


def test_flask_starred_constructor_argument_does_not_establish_application():
    source = SourceFile(
        "web.py",
        "from flask import Flask\n"
        "app = Flask(*[])\n"
        "@app.get('/invalid')\n"
        "def invalid(): return None\n",
    )

    assert FlaskAnalyzer().extract_entities(source) == []


def test_flask_shorthand_rejects_explicit_methods_option():
    source = SourceFile(
        "web.py",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.get('/invalid', methods=['POST'])\n"
        "def invalid(): return None\n",
    )

    assert FlaskAnalyzer().extract_entities(source) == []


def test_flask_class_global_rebinding_invalidates_application_identity():
    source = SourceFile(
        "web.py",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "class Configure:\n"
        "    global app\n"
        "    app = object()\n"
        "@app.get('/stale')\n"
        "def stale(): return None\n",
    )

    assert FlaskAnalyzer().extract_entities(source) == []


def test_django_path_and_re_path_extract_function_and_class_views():
    source = SourceFile(
        "project/urls.py",
        "from django.urls import path, re_path as regex\n"
        "from . import views\n"
        "urlpatterns = [\n"
        "    path('users/', views.list_users, name='users'),\n"
        "    regex(r'^legacy/$', legacy),\n"
        "    path('accounts/', views.AccountView.as_view()),\n"
        "]\n",
    )

    entities = DjangoAnalyzer().extract_entities(source)

    assert [
        (
            entity.framework,
            entity.name,
            entity.target,
            entity.line,
            entity.confidence,
            dict(entity.attributes),
        )
        for entity in entities
    ] == [
        (
            "django",
            "ANY /users/",
            "list_users",
            4,
            "high",
            {
                "handler_kind": "function",
                "method": "ANY",
                "route": "/users/",
                "route_kind": "path",
                "view": "views.list_users",
            },
        ),
        (
            "django",
            "ANY ^legacy/$",
            "legacy",
            5,
            "high",
            {
                "handler_kind": "function",
                "method": "ANY",
                "route": "^legacy/$",
                "route_kind": "re_path",
                "view": "legacy",
            },
        ),
        (
            "django",
            "ANY /accounts/",
            "AccountView",
            6,
            "high",
            {
                "handler_kind": "class",
                "method": "ANY",
                "route": "/accounts/",
                "route_kind": "path",
                "view": "views.AccountView.as_view",
            },
        ),
    ]


def test_django_url_composition_is_conservative_and_evidence_weighted():
    source = SourceFile(
        "urls.py",
        "from django.urls import path\n"
        "prefix = 'api/'\n"
        "path('outside/', outside)\n"
        "urlpatterns = [\n"
        "    path('api/' + 'users/', users),\n"
        "    path(prefix + 'dynamic/', dynamic),\n"
        "    path('included/', include('child.urls')),\n"
        "]\n",
    )

    entities = DjangoAnalyzer().extract_entities(source)

    assert [(entity.name, entity.target, entity.confidence) for entity in entities] == [
        ("ANY /api/users/", "users", "medium")
    ]


@pytest.mark.parametrize(
    "rebind",
    [
        "@(path := replacement)\ndef configure(): pass\n",
        "class Configure:\n    global path\n    path = replacement\n",
    ],
)
def test_django_executable_rebinding_invalidates_url_helper(rebind):
    source = SourceFile(
        "urls.py",
        "from django.urls import path\n"
        f"{rebind}"
        "urlpatterns = [path('users/', users)]\n",
    )

    assert DjangoAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "source, framework",
    [
        (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "def mutate():\n    global app\n    app = object()\n"
            "mutate()\n"
            "@app.get('/x')\ndef route(): pass\n"
            "@app.middleware('http')\nasync def middleware(request, call_next): pass\n",
            "fastapi",
        ),
        (
            "from fastapi import FastAPI\n"
            "def mutate():\n    global FastAPI\n    FastAPI = object()\n"
            "run = mutate\nrun()\n"
            "app = FastAPI()\n@app.get('/x')\ndef route(): pass\n",
            "fastapi",
        ),
        (
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "def mutate():\n    global app\n    app = object()\n"
            "mutate()\n"
            "@app.get('/x')\ndef route(): pass\n"
            "@app.before_request\ndef middleware(): pass\n",
            "flask",
        ),
        (
            "from flask import Flask\n"
            "def mutate():\n    global Flask\n    Flask = object()\n"
            "run = mutate\nrun()\n"
            "app = Flask(__name__)\n@app.get('/x')\ndef route(): pass\n",
            "flask",
        ),
        (
            "from django.urls import path\n"
            "def mutate():\n    global path\n    path = object()\n"
            "run = mutate\nrun()\n"
            "urlpatterns = [path('x/', view)]\n",
            "django",
        ),
        (
            "from fastapi import FastAPI\napp = FastAPI()\n"
            "def mutate():\n    global app\n    app = object()\n"
            "(run := mutate)()\n@app.get('/x')\ndef route(): pass\n"
            "@app.middleware('http')\nasync def middleware(request, call_next): pass\n",
            "fastapi",
        ),
        (
            "from flask import Flask\napp = Flask(__name__)\n"
            "def mutate():\n    global app\n    app = object()\n"
            "(run := mutate)()\n@app.get('/x')\ndef route(): pass\n"
            "@app.before_request\ndef middleware(): pass\n",
            "flask",
        ),
        (
            "from django.urls import path\n"
            "def mutate():\n    global path\n    path = object()\n"
            "(run := mutate)()\nurlpatterns = [path('x/', view)]\n",
            "django",
        ),
        (
            "from fastapi import FastAPI\napp = FastAPI()\nlate()\n"
            "def late():\n    global app\n    app = object()\n"
            "@app.get('/x')\ndef route(): pass\n",
            "fastapi",
        ),
        (
            "from flask import Flask\napp = Flask(__name__)\nlate()\n"
            "def late():\n    global app\n    app = object()\n"
            "@app.get('/x')\ndef route(): pass\n",
            "flask",
        ),
        (
            "from django.urls import path\nlate()\n"
            "def late():\n    global path\n    path = object()\n"
            "urlpatterns = [path('x/', view)]\n",
            "django",
        ),
    ],
)
def test_python_route_framework_called_mutations_invalidate_provenance(
    source, framework
):
    entities = extract_framework_entities(SourceFile("module.py", source))
    assert not [entity for entity in entities if entity.framework == framework]


def test_django_duplicate_route_arguments_are_not_reported():
    source = SourceFile(
        "urls.py",
        "from django.urls import path\n"
        "urlpatterns = [path('users/', users, route='other/')]\n",
    )

    assert DjangoAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "route_call",
    [
        "path('users/', users, *[])",
        "path('users/', users, *[None, 'name', 'extra'])",
        "path('users/', users, None, 'name', 'extra')",
    ],
)
def test_django_invalid_positional_route_arguments_are_not_reported(route_call):
    source = SourceFile(
        "urls.py",
        "from django.urls import path\n"
        f"urlpatterns = [{route_call}]\n",
    )

    assert DjangoAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize("operator", ["-=", "|=", "*="])
def test_django_only_additive_urlpattern_updates_are_reported(operator):
    source = SourceFile(
        "urls.py",
        "from django.urls import path\n"
        "urlpatterns = []\n"
        f"urlpatterns {operator} [path('users/', users)]\n",
    )

    assert DjangoAnalyzer().extract_entities(source) == []


def test_express_application_and_router_routes_preserve_middleware_ownership():
    source = SourceFile(
        "server.js",
        "import express from 'express';\n"
        "const app = express();\n"
        "const router = express.Router();\n"
        "function auth() {}\n"
        "function listUsers() {}\n"
        "function createUser() {}\n"
        "app.get('/users', auth, listUsers);\n"
        "router.post('/users', createUser);\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (
            entity.framework,
            entity.name,
            entity.target,
            entity.line,
            entity.end_line,
            entity.confidence,
            dict(entity.attributes),
        )
        for entity in entities
    ] == [
        (
            "express",
            "GET /users",
            "listUsers",
            7,
            7,
            "high",
            {
                "handler": "listUsers",
                "method": "GET",
                "middleware": "auth",
                "owner": "app",
                "route": "/users",
            },
        ),
        (
            "express",
            "POST /users",
            "createUser",
            8,
            8,
            "high",
            {
                "handler": "createUser",
                "method": "POST",
                "middleware": "",
                "owner": "router",
                "route": "/users",
            },
        ),
    ]


def test_express_aliases_dynamic_routes_and_rebinding_fail_closed():
    source = SourceFile(
        "server.ts",
        "import web from 'express';\n"
        "const app = web();\n"
        "app.patch('/ready', guards.auth, controllers.ready);\n"
        "const route = '/users/' + userId;\n"
        "app.get(route, controllers.user);\n"
        "app = replacement;\n"
        "app.delete('/stale', controllers.stale);\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target, dict(entity.attributes)) for entity in entities] == [
        (
            "PATCH /ready",
            "ready",
            {
                "handler": "controllers.ready",
                "method": "PATCH",
                "middleware": "guards.auth",
                "owner": "app",
                "route": "/ready",
            },
        )
    ]


def test_express_commonjs_constructor_establishes_application_and_router():
    source = SourceFile(
        "server.cjs",
        "const web = require('express');\n"
        "const app = web();\n"
        "const router = web.Router();\n"
        "app.get('/health', health);\n"
        "router.post('/users', createUser);\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /health", "health"),
        ("POST /users", "createUser"),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "app.get = replacement;",
        "configure(app = replacement);",
    ],
)
def test_express_application_mutations_invalidate_route_identity(mutation):
    source = SourceFile(
        "server.js",
        "import express from 'express';\n"
        "const app = express();\n"
        f"{mutation}\n"
        "app.get('/stale', stale);\n",
    )

    assert extract_framework_entities(source) == []


def test_express_deferred_function_mutation_does_not_invalidate_application():
    source = SourceFile(
        "server.js",
        "import express from 'express';\n"
        "const app = express();\n"
        "function configure() { app = replacement; }\n"
        "app.get('/health', health);\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /health", "health")
    ]


def test_express_deferred_generator_mutation_does_not_invalidate_application():
    source = SourceFile(
        "server.js",
        "import express from 'express';\n"
        "const app = express();\n"
        "const deferred = function* () { app = replacement; };\n"
        "app.get('/health', health);\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /health", "health")
    ]


@pytest.mark.parametrize("path", ["server.mts", "server.cts"])
def test_express_omits_unsupported_module_suffixes(path):
    source = SourceFile(
        path,
        "import express from 'express';\n"
        "const app = express();\n"
        "app.get('/health', health);\n",
    )

    assert extract_framework_entities(source) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "(() => { app = replacement; })();",
        "(0, () => { app = replacement; })();",
        "(() => { app = replacement; }).call(null);",
        "function configure() { app = replacement; } configure();",
        "const configure = () => { app = replacement; }; configure();",
        "function configure() { app = replacement; } configure.call(null);",
        "function configure() { app = replacement; } (0, configure)();",
        "function configure() { app = replacement; } const run = configure; run();",
        "function outer() { function inner() { app = replacement; } inner(); } outer();",
        "function outer() { const inner = function* () {}; } outer();",
        "function configure() { app = replacement; } let run; run = configure; run();",
        "function configure() {} function configure() { app = replacement; } configure();",
        "app++;",
        "app += replacement;",
        "({x: app} = value);",
        "var {x: app} = value;",
        "app['get'] = replacement;",
        "delete app.get;",
        "delete app['get'];",
    ],
)
def test_express_executed_mutation_shapes_invalidate_application(mutation):
    source = SourceFile(
        "server.js",
        "import express from 'express';\n"
        "const app = express();\n"
        f"{mutation}\n"
        "app.get('/stale', stale);\n",
    )

    assert extract_framework_entities(source) == []


def test_nestjs_controller_prefix_and_method_decorators_preserve_ownership():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller as Resource, Get as Read, Post } "
        "from '@nestjs/common';\n"
        "@Resource('users')\n"
        "export class UsersController {\n"
        "  @Read()\n"
        "  list() {}\n"
        "  @Read(':id')\n"
        "  getOne() {}\n"
        "  @Post()\n"
        "  create() {}\n"
        "}\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (
            entity.framework,
            entity.name,
            entity.target,
            entity.line,
            entity.end_line,
            dict(entity.attributes),
        )
        for entity in entities
    ] == [
        (
            "nestjs",
            "GET /users",
            "list",
            4,
            5,
            {
                "controller": "UsersController",
                "handler": "UsersController.list",
                "handler_kind": "method",
                "method": "GET",
                "route": "/users",
            },
        ),
        (
            "nestjs",
            "GET /users/:id",
            "getOne",
            6,
            7,
            {
                "controller": "UsersController",
                "handler": "UsersController.getOne",
                "handler_kind": "method",
                "method": "GET",
                "route": "/users/:id",
            },
        ),
        (
            "nestjs",
            "POST /users",
            "create",
            8,
            9,
            {
                "controller": "UsersController",
                "handler": "UsersController.create",
                "handler_kind": "method",
                "method": "POST",
                "route": "/users",
            },
        ),
    ]


def test_nestjs_dynamic_controller_and_method_routes_are_omitted():
    source = SourceFile(
        "health.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        "const prefix = 'dynamic';\n"
        "const route = 'ready';\n"
        "@Controller(prefix)\n"
        "class DynamicController {\n"
        "  @Get('status')\n"
        "  status() {}\n"
        "}\n"
        "@Controller('health')\n"
        "class HealthController {\n"
        "  @Get(route)\n"
        "  dynamic() {}\n"
        "  @Get()\n"
        "  ready() {}\n"
        "}\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target, entity.line) for entity in entities] == [
        ("GET /health", "ready", 13)
    ]


def test_nestjs_rebound_decorator_does_not_establish_route_identity():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        "Get = replacement;\n"
        "@Controller('users')\n"
        "class UsersController {\n"
        "  @Get()\n"
        "  list() {}\n"
        "}\n",
    )

    assert extract_framework_entities(source) == []


def test_nestjs_invalid_recognized_decorator_invalidates_method_evidence():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get, Post } from '@nestjs/common';\n"
        "const route = 'dynamic';\n"
        "@Controller('users')\n"
        "class UsersController {\n"
        "  @Get(route)\n"
        "  @Post('create')\n"
        "  create() {}\n"
        "  @Get()\n"
        "  list() {}\n"
        "}\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /users", "list")
    ]


def test_nestjs_deferred_method_mutation_does_not_invalidate_decorator():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        "@Controller('users')\n"
        "class UsersController {\n"
        "  @Get()\n"
        "  list() { Get = replacement; }\n"
        "}\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /users", "list")
    ]


def test_nestjs_deferred_generator_mutation_does_not_invalidate_decorator():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        "const deferred = function* () { Get = replacement; };\n"
        "@Controller('users')\n"
        "class UsersController { @Get() list() {} }\n",
    )

    entities = extract_framework_entities(source)

    assert [(entity.name, entity.target) for entity in entities] == [
        ("GET /users", "list")
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "(() => { Get = replacement; })();",
        "(0, () => { Get = replacement; })();",
        "(() => { Get = replacement; }).call(null);",
        "function configure() { Get = replacement; } configure();",
        "const configure = () => { Get = replacement; }; configure();",
        "function configure() { Get = replacement; } configure.call(null);",
        "function configure() { Get = replacement; } (0, configure)();",
        "function configure() { Get = replacement; } const run = configure; run();",
        "function outer() { function inner() { Get = replacement; } inner(); } outer();",
        "function outer() { const inner = function* () {}; } outer();",
        "function configure() { Get = replacement; } let run; run = configure; run();",
        "function configure() {} function configure() { Get = replacement; } configure();",
        "Get++;",
        "Get += replacement;",
        "({Get} = value);",
    ],
)
def test_nestjs_executed_mutation_shapes_invalidate_decorator(mutation):
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        f"{mutation}\n"
        "@Controller('users')\n"
        "class UsersController {\n"
        "  @Get()\n"
        "  list() {}\n"
        "}\n",
    )

    assert extract_framework_entities(source) == []


@pytest.mark.parametrize(
    "class_source",
    [
        "class Before { static effect = (Get = replacement); }\n"
        "@Controller('users')\n"
        "class UsersController { @Get() list() {} }\n",
        "@Controller('users')\n"
        "class UsersController {\n"
        "  static effect = (Get = replacement);\n"
        "  @Get() list() {}\n"
        "}\n",
        "@Controller('users')\n"
        "class UsersController {\n"
        "  @mutate(Get = replacement) other() {}\n"
        "  @Get() list() {}\n"
        "}\n",
    ],
)
def test_nestjs_executed_class_effects_invalidate_decorator_evidence(class_source):
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n" + class_source,
    )

    assert extract_framework_entities(source) == []


@pytest.mark.parametrize(
    "method_source",
    [
        "@Get() static list() {}",
        "@Get() get list() { return []; }",
        "@Get() set list(value) {}",
        "@Get() *list() {}",
    ],
)
def test_nestjs_static_methods_and_accessors_are_not_route_handlers(method_source):
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get } from '@nestjs/common';\n"
        "@Controller('users')\n"
        f"class UsersController {{ {method_source} }}\n",
    )

    assert extract_framework_entities(source) == []


def test_express_and_nestjs_routes_persist_and_trace_owned_handlers(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    (repository / "server.js").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "function expressHelper() {}\n"
        "function listUsers() { return expressHelper(); }\n"
        "app.get('/express-users', listUsers);\n",
        encoding="utf-8",
    )
    (repository / "users.controller.ts").write_text(
        "import { Controller, Get } from '@nestjs/common';\n"
        "function nestHelper() {}\n"
        "@Controller('nest-users')\n"
        "class UsersController {\n"
        "  @Get()\n"
        "  list() { return nestHelper(); }\n"
        "}\n",
        encoding="utf-8",
    )

    build_index(repository)

    routes = find_framework_entities(repository, kind="route")
    assert [(item["framework"], item["name"], item["target"]) for item in routes] == [
        ("express", "GET /express-users", "listUsers"),
        ("nestjs", "GET /nest-users", "list"),
    ]

    express_nodes, express_edges = trace_symbol(repository, "GET /express-users")
    assert [node["name"] for node in express_nodes] == [
        "GET /express-users",
        "listUsers",
        "expressHelper",
    ]
    assert [(edge["kind"], edge["to"], edge["resolved"]) for edge in express_edges] == [
        ("handles", "listUsers", True),
        ("calls", "expressHelper", True),
    ]

    nest_nodes, nest_edges = trace_symbol(repository, "GET /nest-users")
    assert [node["name"] for node in nest_nodes] == [
        "GET /nest-users",
        "list",
        "nestHelper",
    ]
    assert [(edge["kind"], edge["to"], edge["resolved"]) for edge in nest_edges] == [
        ("handles", "list", True),
        ("calls", "nestHelper", True),
    ]


def test_same_line_duplicate_route_registrations_trace_distinct_handlers(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "server.js").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "function first() {}\n"
        "function second() {}\n"
        "app.get('/users', first); app.get('/users', second);\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    nodes, edges = trace_symbol(repository, "GET /users")

    route_nodes = [node for node in nodes if node["kind"] == "route"]
    handle_edges = [edge for edge in edges if edge["kind"] == "handles"]
    assert [node["target"] for node in route_nodes] == ["first", "second"]
    assert [(edge["to"], edge["resolved"]) for edge in handle_edges] == [
        ("first", True),
        ("second", True),
    ]


def test_same_position_framework_routes_preserve_registration_identity(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    route_file = repository / "app" / "api" / "users" / "route.ts"
    route_file.parent.mkdir(parents=True)
    route_file.write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "export function GET() {}; app.get('/api/users', GET);\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    build_index(repository)

    routes = find_framework_entities(repository, kind="route")
    assert [
        (route["framework"], route["name"], route["target"], route["line"])
        for route in routes
    ] == [
        ("express", "GET /api/users", "GET", 3),
        ("nextjs", "GET /api/users", "GET", 3),
    ]
    with closing(sqlite3.connect(intelligence.index_path(repository))) as connection:
        raw_confidences = [
            row[0]
            for row in connection.execute(
                "SELECT confidence FROM relationships WHERE kind='handles' "
                "ORDER BY confidence"
            )
        ]
        assert len(set(raw_confidences)) == 2
    relationships = find_relationships(repository, kind="handles")
    assert [relationship["confidence"] for relationship in relationships] == [
        "high",
        "high",
    ]

    nodes, edges = trace_symbol(repository, "GET /api/users")
    assert [node["framework"] for node in nodes if node["kind"] == "route"] == [
        "express",
        "nextjs",
    ]
    assert [
        (edge["to"], edge["resolved"], edge.get("reason"))
        for edge in edges
        if edge["kind"] == "handles"
    ] == [("GET", True, None), ("GET", True, None)]


def test_framework_query_rebuilds_logically_corrupt_attributes(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "schema.prisma").write_text(
        "model User {\n  id Int @id\n}\n", encoding="utf-8"
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    database = intelligence.index_path(repository)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("UPDATE framework_entities SET attributes='{invalid'")
        connection.commit()

    entities = find_framework_entities(repository, kind="model")

    assert [entity["name"] for entity in entities] == ["User"]
    assert entities[0]["attributes"] == {"model": "User", "table": "User"}


@pytest.mark.parametrize(
    "corruption",
    [
        "UPDATE framework_entities SET confidence='certain'",
        "UPDATE framework_entities "
        "SET attributes='{\"model\":\"User\",\"model\":\"Wrong\",\"table\":\"User\"}'",
    ],
)
def test_framework_query_rebuilds_semantically_corrupt_rows(
    tmp_path, monkeypatch, corruption
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "schema.prisma").write_text(
        "model User {\n  id Int @id\n}\n", encoding="utf-8"
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    with closing(sqlite3.connect(intelligence.index_path(repository))) as connection:
        connection.execute(corruption)
        connection.commit()

    entities = find_framework_entities(repository, kind="model")

    assert [(entity["confidence"], entity["attributes"]) for entity in entities] == [
        ("high", {"model": "User", "table": "User"})
    ]


@pytest.mark.parametrize(
    "corruption",
    [
        "DELETE FROM relationships WHERE kind='handles' AND target='first'",
        "INSERT INTO relationships(source, target, kind, path, line, confidence) "
        "SELECT source, target, kind, path, line, confidence FROM relationships "
        "WHERE kind='handles' AND target='first'",
    ],
)
def test_same_line_route_corrupt_relationships_rebuild_cache(
    tmp_path, monkeypatch, corruption
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "server.js").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "function first() {}\n"
        "function second() {}\n"
        "app.get('/users', first); app.get('/users', second);\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    with sqlite3.connect(intelligence.index_path(repository)) as connection:
        connection.execute(corruption)
        connection.commit()

    _nodes, edges = trace_symbol(repository, "GET /users")

    assert [
        (edge["to"], edge["resolved"])
        for edge in edges
        if edge["kind"] == "handles"
    ] == [("first", True), ("second", True)]


def test_flask_and_django_routes_use_shared_index_relationship_flow(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "web.py").write_text(
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.get('/health')\n"
        "def health(): return probe()\n"
        "def probe(): return None\n",
        encoding="utf-8",
    )
    (repository / "urls.py").write_text(
        "from django.urls import path\n"
        "from .views import AccountView\n"
        "urlpatterns = [path('accounts/', AccountView.as_view())]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    build_index(repository)

    assert [
        (entity["framework"], entity["name"], entity["target"])
        for entity in find_framework_entities(repository, kind="route")
    ] == [
        ("django", "ANY /accounts/", "AccountView"),
        ("flask", "GET /health", "health"),
    ]
    assert [
        (edge["source"], edge["target"], edge["kind"])
        for edge in find_relationships(repository, kind="handles")
    ] == [
        ("ANY /accounts/", "AccountView", "handles"),
        ("GET /health", "health", "handles"),
    ]
    nodes, edges = trace_symbol(repository, "GET /health")
    assert [node["name"] for node in nodes] == ["GET /health", "health", "probe"]
    assert [(edge["kind"], edge["to"]) for edge in edges] == [
        ("handles", "health"),
        ("calls", "probe"),
    ]


def test_trace_django_route_resolves_unique_cross_file_view(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n"
        "urlpatterns = [\n"
        "    path('users/', views.list_users),\n"
        "    path('accounts/', views.AccountView.as_view()),\n"
        "]\n",
        encoding="utf-8",
    )
    (repository / "views.py").write_text(
        "def helper(): return []\n"
        "def list_users(): return helper()\n"
        "class AccountView: pass\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    function_nodes, function_edges = trace_symbol(repository, "ANY /users/")
    class_nodes, class_edges = trace_symbol(repository, "ANY /accounts/")

    assert [node["name"] for node in function_nodes] == [
        "ANY /users/",
        "list_users",
        "helper",
    ]
    assert [(edge["kind"], edge["to"], edge["resolved"]) for edge in function_edges] == [
        ("handles", "list_users", True),
        ("calls", "helper", True),
    ]
    assert [node["name"] for node in class_nodes] == [
        "ANY /accounts/",
        "AccountView",
    ]
    assert [(edge["kind"], edge["to"], edge["resolved"]) for edge in class_edges] == [
        ("handles", "AccountView", True)
    ]


def test_trace_django_dotted_view_does_not_resolve_unrelated_leaf_name(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n"
        "urlpatterns = [path('x/', views.target)]\n",
        encoding="utf-8",
    )
    (repository / "views.py").write_text("def other(): pass\n", encoding="utf-8")
    (repository / "unrelated.py").write_text(
        "def target(): pass\n", encoding="utf-8"
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    nodes, edges = trace_symbol(repository, "ANY /x/")

    assert [node["name"] for node in nodes] == ["ANY /x/"]
    assert [(edge["to"], edge["resolved"], edge["reason"]) for edge in edges] == [
        ("target", False, "handler_not_found")
    ]


def test_route_trace_prefers_unique_same_file_call_target(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "server.js").write_text(
        "import express from 'express';\n"
        "const app = express();\n"
        "function handler() { return helper(); }\n"
        "function helper() {}\n"
        "app.get('/x', handler);\n",
        encoding="utf-8",
    )
    (repository / "other.js").write_text(
        "function helper() { return unrelated(); }\n"
        "function unrelated() {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    nodes, edges = trace_symbol(repository, "GET /x")

    assert [node["name"] for node in nodes] == ["GET /x", "handler", "helper"]
    assert [(edge["from"], edge["to"]) for edge in edges] == [
        ("GET /x", "handler"),
        ("handler", "helper"),
    ]


def test_fastapi_decorator_before_application_assignment_is_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n"
        "app = FastAPI()\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_fastapi_application_reassignment_invalidates_route_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "app = object()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_conditional_fastapi_reassignment_makes_route_identity_ambiguous():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "if use_mock:\n"
        "    app = object()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "compound_statement",
    [
        "if enabled:\n    import client as app\n",
        "if enabled:\n    del app\n",
        "if enabled:\n    def app(): pass\n",
        "if enabled:\n    class app: pass\n",
        "try:\n    operation()\nexcept Exception as app:\n    pass\n",
        "match value:\n    case app:\n        pass\n",
    ],
)
def test_compound_binding_invalidates_fastapi_identity(compound_statement):
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        f"{compound_statement}"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_destructured_constructor_result_does_not_establish_application_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app, other = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_destructured_delete_removes_fastapi_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "other = object()\n"
        "del (app, other)\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "binding_statement",
    [
        "from extensions import *\n",
        "[(app := value) for value in values]\n",
        "def helper(value=(app := object())): pass\n",
        "(lambda value=(app := object()): value)\n",
        "class Helper((app := Base)): pass\n",
        "class Helper:\n    global app\n    app = object()\n",
    ],
)
def test_enclosing_scope_binding_invalidates_fastapi_identity(binding_statement):
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        f"{binding_statement}"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_nested_fastapi_name_shadow_is_not_reported_as_module_route():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "def configure():\n"
        "    app = object()\n"
        "    @app.get('/users')\n"
        "    def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_fastapi_router_prefix_is_included_in_route_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix='/api')\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    entities = FastAPIAnalyzer().extract_entities(source)

    assert [(entity.name, dict(entity.attributes)) for entity in entities] == [
        ("GET /api/users", {"method": "GET", "route": "/api/users"})
    ]


def test_dynamic_fastapi_router_prefix_is_not_reported_as_literal_route():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(prefix=API_PREFIX)\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_literal_unpacked_fastapi_router_prefix_is_preserved():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(**{'prefix': '/api'})\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    entities = FastAPIAnalyzer().extract_entities(source)

    assert [(entity.name, dict(entity.attributes)) for entity in entities] == [
        ("GET /api/users", {"method": "GET", "route": "/api/users"})
    ]


def test_literal_unpacked_router_options_reject_unrelated_values():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(**{'prefix': '/api', 'tags': tags})\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_dynamic_unpacked_fastapi_router_options_are_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(**options)\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize("prefix", ["api", "/api/"])
def test_invalid_static_fastapi_router_prefix_is_not_reported(prefix):
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        f"router = APIRouter(prefix={prefix!r})\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_duplicate_unpacked_fastapi_router_keyword_is_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter(tags=[], **{'tags': []})\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_positional_fastapi_router_options_are_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import APIRouter\n"
        "router = APIRouter('/api')\n"
        "@router.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_fastapi_api_route_extracts_each_literal_http_method():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.api_route('/users', methods=['GET', 'POST'])\n"
        "def users(): return []\n",
    )

    entities = FastAPIAnalyzer().extract_entities(source)

    assert [(entity.name, dict(entity.attributes)) for entity in entities] == [
        ("GET /users", {"method": "GET", "route": "/users"}),
        ("POST /users", {"method": "POST", "route": "/users"}),
    ]


def test_fastapi_api_route_deduplicates_normalized_methods():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.api_route('/users', methods=['GET', 'get'])\n"
        "def users(): return []\n",
    )

    assert [entity.name for entity in FastAPIAnalyzer().extract_entities(source)] == [
        "GET /users"
    ]


def test_fastapi_http_decorator_with_unpacked_options_is_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users', **options)\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_fastapi_http_decorator_with_extra_positional_argument_is_not_reported():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users', '/duplicate')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "loop_statement",
    [
        "for item in items:\n    app = object()\n    break\nelse:\n    app = FastAPI()\n",
        "while enabled:\n    app = object()\n    break\nelse:\n    app = FastAPI()\n",
    ],
)
def test_loop_control_flow_invalidates_fastapi_identity(loop_statement):
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        f"{loop_statement}"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_try_control_flow_invalidates_fastapi_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "try:\n"
        "    app = object()\n"
        "    risky()\n"
        "    app = FastAPI()\n"
        "except Exception:\n"
        "    pass\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "compound_statement",
    [
        "for item in items:\n"
        "    app = object()\n"
        "    continue\n"
        "    app = FastAPI()\n",
        "try:\n"
        "    if enabled:\n"
        "        app = object()\n"
        "        risky()\n"
        "        app = FastAPI()\n"
        "except Exception:\n"
        "    pass\n",
        "with suppress(Exception):\n"
        "    app = object()\n"
        "    risky()\n"
        "    app = FastAPI()\n",
    ],
)
def test_complex_control_flow_invalidates_fastapi_identity(compound_statement):
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        f"{compound_statement}"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_exception_group_try_invalidates_fastapi_identity():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "try:\n"
        "    pass\n"
        "except* Exception:\n"
        "    pass\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


@pytest.mark.parametrize(
    "invalid_source",
    [
        "from fastapi import FastAPI\n"
        "app = FastAPI(title='one', title='two')\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.api_route('/users', methods=['GET'], methods=['POST'])\n"
        "def list_users(): return []\n",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users', tags=[], tags=[])\n"
        "def list_users(): return []\n",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "continue\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "break\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "return\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    ],
)
def test_compile_invalid_python_produces_no_routes(invalid_source):
    assert FastAPIAnalyzer().extract_entities(SourceFile("api.py", invalid_source)) == []


def test_unrelated_decorator_and_malformed_python_produce_no_routes():
    unrelated = SourceFile(
        "api.py",
        "class Client:\n"
        "    def get(self, path): return lambda function: function\n"
        "app = Client()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )
    malformed = SourceFile("broken.py", "from fastapi import FastAPI\n@app.get(\n")

    analyzer = FastAPIAnalyzer()

    assert analyzer.extract_entities(unrelated) == []
    assert analyzer.extract_entities(malformed) == []


def test_relative_fastapi_named_module_does_not_establish_framework_identity():
    source = SourceFile(
        "api.py",
        "from .fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
    )

    assert FastAPIAnalyzer().extract_entities(source) == []


def test_fastapi_routes_are_persisted_and_replaced_incrementally(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "api.py"
    source.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    first = build_index(repository)

    assert first["framework_entities"] == 1
    assert find_framework_entities(repository, kind="route") == [
        {
            "framework": "fastapi",
            "kind": "route",
            "name": "GET /users",
            "target": "list_users",
            "path": "api.py",
            "line": 3,
            "end_line": 4,
            "confidence": "high",
            "attributes": {"method": "GET", "route": "/users"},
        }
    ]

    source.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.post('/users')\n"
        "def create_user(): return {}\n",
        encoding="utf-8",
    )

    second = build_index(repository)

    assert second["framework_entities"] == 1
    assert [item["name"] for item in find_framework_entities(repository)] == [
        "POST /users"
    ]
    assert [
        (item["source"], item["target"])
        for item in find_relationships(repository, kind="handles")
    ] == [("POST /users", "create_user")]


def test_fastapi_route_persists_handles_relationship(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    build_index(repository)

    assert find_relationships(
        repository, source="GET /users", kind="handles"
    ) == [
        {
            "source": "GET /users",
            "target": "list_users",
            "kind": "handles",
            "path": "api.py",
            "line": 3,
            "confidence": "high",
        }
    ]


def test_trace_route_continues_through_handler_calls(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return load_users()\n"
        "def load_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    nodes, edges = trace_symbol(repository, "GET /users")

    assert {(node["name"], node["kind"]) for node in nodes} == {
        ("GET /users", "route"),
        ("list_users", "function"),
        ("load_users", "function"),
    }
    assert {
        (edge["from"], edge["to"], edge.get("kind"), edge.get("resolved"))
        for edge in edges
    } == {
        ("GET /users", "list_users", "handles", True),
        ("list_users", "load_users", "calls", True),
    }


def test_trace_route_respects_non_positive_depth_boundaries(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return load_users()\n"
        "def load_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    zero_nodes, zero_edges = trace_symbol(repository, "GET /users", max_depth=0)
    negative_nodes, negative_edges = trace_symbol(
        repository, "GET /users", max_depth=-1
    )

    assert [node["name"] for node in zero_nodes] == ["GET /users"]
    assert [(edge["kind"], edge["resolved"]) for edge in zero_edges] == [
        ("handles", True)
    ]
    assert negative_nodes == []
    assert negative_edges == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            "DELETE FROM symbols WHERE name = 'list_users'",
            "handler_not_found",
        ),
        (
            "INSERT INTO symbols(name, kind, path, line, end_line, parent, signature) "
            "VALUES ('list_users', 'function', 'api.py', 4, 4, NULL, '')",
            "handler_ambiguous",
        ),
        (
            "UPDATE symbols SET kind = 'class' WHERE name = 'list_users'",
            "handler_not_found",
        ),
    ],
)
def test_trace_route_reports_unresolved_handler(
    tmp_path, monkeypatch, mutation, reason
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    with closing(sqlite3.connect(intelligence.index_path(repository))) as connection:
        connection.execute(mutation)
        connection.commit()

    nodes, edges = trace_symbol(repository, "GET /users")

    assert [(node["name"], node["kind"]) for node in nodes] == [
        ("GET /users", "route")
    ]
    assert edges == [
        {
            "from": "GET /users",
            "to": "list_users",
            "kind": "handles",
            "path": "api.py",
            "from_line": 3,
            "resolved": False,
            "reason": reason,
        }
    ]
    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "trace", "GET /users"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert f"unresolved: {reason}" in result.stdout


def test_trace_orders_calls_by_source_evidence(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    zebra()\n"
        "    alpha()\n"
        "def zebra(): return None\n"
        "def alpha(): return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)
    database = intelligence.index_path(repository)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "DELETE FROM relationships WHERE source='list_users' AND kind='calls'"
        )
        connection.executemany(
            "INSERT INTO relationships(source, target, kind, path, line, confidence) "
            "VALUES (?, ?, 'calls', 'api.py', ?, 'high')",
            [
                ("list_users", "alpha", 6),
                ("list_users", "zebra", 5),
            ],
        )
        connection.commit()

    _nodes, edges = trace_symbol(repository, "GET /users")

    assert [edge["to"] for edge in edges if edge["kind"] == "calls"] == [
        "zebra",
        "alpha",
    ]


def test_trace_route_cycle_processes_each_definition_once(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    zebra()\n"
        "    alpha()\n"
        "def zebra(): return None\n"
        "def alpha(): return list_users()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    _nodes, edges = trace_symbol(repository, "GET /users")

    assert [
        (edge["from"], edge["to"])
        for edge in edges
        if edge["kind"] == "calls"
    ] == [
        ("list_users", "zebra"),
        ("list_users", "alpha"),
        ("alpha", "list_users"),
    ]


def test_deleted_source_removes_persisted_framework_entities(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    source = repository / "api.py"
    source.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    build_index(repository)

    source.unlink()
    result = build_index(repository)

    assert result["removed"] == 1
    assert result["framework_entities"] == 0
    assert find_framework_entities(repository) == []


def test_framework_entity_filters_are_combined_and_case_insensitive(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    matches = find_framework_entities(
        repository,
        framework="FASTAPI",
        kind="ROUTE",
        name="get /USERS",
    )

    assert [item["target"] for item in matches] == ["list_users"]
    assert find_framework_entities(repository, framework="flask", kind="route") == []


def test_schema_generation_two_index_rebuilds_with_framework_entities(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    database = intelligence.index_path(repository)
    database.parent.mkdir(parents=True)
    intelligence._initialize_database(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP INDEX framework_entities_kind")
        connection.execute("DROP INDEX framework_entities_name")
        connection.execute("DROP TABLE framework_entities")
        connection.execute("PRAGMA user_version = 2")

    result = build_index(repository)

    assert result["framework_entities"] == 1
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT name FROM framework_entities"
        ).fetchone()[0] == "GET /users"


def test_routes_command_returns_fastapi_routes_as_stable_json(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health(): return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "routes", "--format", "json"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "routes"
    assert payload["routes"] == [
        {
            "framework": "fastapi",
            "kind": "route",
            "name": "GET /health",
            "target": "health",
            "path": "api.py",
            "line": 3,
            "end_line": 4,
            "confidence": "high",
            "attributes": {"method": "GET", "route": "/health"},
        }
    ]


def test_trace_command_starts_from_fastapi_route_as_stable_json(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/users')\n"
        "def list_users(): return load_users()\n"
        "def load_users(): return []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))
    command = [
        sys.executable,
        "-m",
        "contextforge",
        "trace",
        "GET /users",
        "--format",
        "json",
    ]

    first = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    second = subprocess.run(
        command,
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["query"] == "GET /users"
    assert [(node["name"], node["kind"]) for node in payload["nodes"]] == [
        ("GET /users", "route"),
        ("list_users", "function"),
        ("load_users", "function"),
    ]
    assert [
        (edge["from"], edge["to"], edge["kind"], edge["resolved"])
        for edge in payload["edges"]
    ] == [
        ("GET /users", "list_users", "handles", True),
        ("list_users", "load_users", "calls", True),
    ]


def test_index_text_output_includes_framework_entity_count(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health(): return {'status': 'ok'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "index"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert "Framework Entities: 1" in result.stdout


def test_models_command_returns_sqlalchemy_models_as_stable_json(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "models.py").write_text(
        "from sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "\n"
        "class User(Base):\n"
        "    __tablename__ = 'users'\n"
        "    id = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "models", "--format", "json"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "models"
    assert payload["models"] == [
        {
            "framework": "sqlalchemy",
            "kind": "model",
            "name": "User",
            "target": "users",
            "path": "models.py",
            "line": 4,
            "end_line": 6,
            "confidence": "high",
            "attributes": {"base": "Base", "table": "users"},
        }
    ]


def test_sqlalchemy_legacy_declarative_base_extracts_literal_table_model():
    source = SourceFile(
        "models.py",
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base()\n"
        "class Invoice(Base):\n"
        "    __tablename__ = 'invoices'\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target)
        for entity in entities
    ] == [("sqlalchemy", "model", "Invoice", "invoices")]


def test_jobs_command_returns_celery_tasks_as_stable_json(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "tasks.py").write_text(
        "from celery import Celery\n"
        "app = Celery('worker')\n"
        "@app.task(name='mail.send')\n"
        "def send_mail(): return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "jobs", "--format", "json"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "jobs"
    assert payload["jobs"] == [
        {
            "framework": "celery",
            "kind": "job",
            "name": "send_mail",
            "target": "mail.send",
            "path": "tasks.py",
            "line": 3,
            "end_line": 4,
            "confidence": "high",
            "attributes": {"application": "app", "task": "mail.send"},
        }
    ]


def test_services_command_returns_sqlalchemy_repositories_as_stable_json(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "repositories.py").write_text(
        "from sqlalchemy.orm import Session\n"
        "class UserRepository:\n"
        "    def __init__(self, session: Session):\n"
        "        self.session = session\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "services", "--format", "json"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "services"
    assert payload["services"] == [
        {
            "framework": "sqlalchemy",
            "kind": "repository",
            "name": "UserRepository",
            "target": "Session",
            "path": "repositories.py",
            "line": 2,
            "end_line": 4,
            "confidence": "high",
            "attributes": {"session": "Session"},
        }
    ]


def test_sqlalchemy_module_alias_extracts_model_and_repository():
    source = SourceFile(
        "data.py",
        "import sqlalchemy.orm as orm\n"
        "class Base(orm.DeclarativeBase): pass\n"
        "class Widget(Base):\n"
        "    __tablename__ = 'widgets'\n"
        "class WidgetRepository:\n"
        "    def __init__(self, session: orm.Session):\n"
        "        self.session = session\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        ("model", "Widget", "widgets", {"base": "Base", "table": "widgets"}),
        (
            "repository",
            "WidgetRepository",
            "orm.Session",
            {"session": "orm.Session"},
        ),
    ]


def test_celery_shared_task_alias_extracts_job():
    source = SourceFile(
        "tasks.py",
        "from celery import shared_task as background\n"
        "@background(name='reports.build')\n"
        "def build_report(): return None\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "job",
            "build_report",
            "reports.build",
            {"application": "shared", "task": "reports.build"},
        )
    ]


def test_react_import_extracts_jsx_component_and_hook():
    source = SourceFile(
        "src/UserCard.tsx",
        "import React, { useState } from 'react';\n"
        "export function UserCard() { return <article />; }\n"
        "export const useUser = () => useState(null);\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target)
        for entity in entities
    ] == [
        ("react", "component", "UserCard", None),
        ("react", "hook", "useUser", "useState"),
    ]


def test_nextjs_app_route_extracts_exported_http_handler():
    source = SourceFile(
        "app/api/users/route.ts",
        "export async function GET() { return Response.json([]); }\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "nextjs",
            "route",
            "GET /api/users",
            "GET",
            {"method": "GET", "route": "/api/users"},
        )
    ]


def test_nextjs_use_server_module_extracts_exported_action():
    source = SourceFile(
        "app/actions.ts",
        "'use server';\n"
        "export async function createUser() { return null; }\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, dict(entity.attributes))
        for entity in entities
    ] == [
        ("nextjs", "action", "createUser", {"directive": "use server"})
    ]


def test_angular_core_decorators_extract_component_and_service():
    source = SourceFile(
        "src/app/users.ts",
        "import { Component, Injectable } from '@angular/core';\n"
        "@Component({ selector: 'app-users' })\n"
        "export class UsersComponent {}\n"
        "@Injectable({ providedIn: 'root' })\n"
        "export class UsersService {}\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "angular",
            "component",
            "UsersComponent",
            "app-users",
            {"selector": "app-users"},
        ),
        (
            "angular",
            "service",
            "UsersService",
            "root",
            {"provided_in": "root"},
        ),
    ]


def test_angular_routes_binding_extracts_literal_component_route():
    source = SourceFile(
        "src/app/app.routes.ts",
        "import { Routes } from '@angular/router';\n"
        "export const routes: Routes = [\n"
        "  { path: 'users', component: UsersComponent },\n"
        "];\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "angular",
            "route",
            "ANY /users",
            "UsersComponent",
            {"handler": "UsersComponent", "method": "ANY", "route": "/users"},
        )
    ]


def test_prisma_schema_models_are_scanned_and_extracted():
    assert ".prisma" in CODE_EXTENSIONS
    source = SourceFile(
        "prisma/schema.prisma",
        "model User {\n"
        "  id Int @id\n"
        "  email String @unique\n"
        "  @@map(\"users\")\n"
        "}\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "prisma",
            "model",
            "User",
            "users",
            {"model": "User", "table": "users"},
        )
    ]


def test_typeorm_entity_and_repository_extract_proven_model_relationship():
    source = SourceFile(
        "src/user.entity.ts",
        "import { Entity, Repository } from 'typeorm';\n"
        "@Entity('users')\n"
        "export class User {}\n"
        "export class UserRepository extends Repository<User> {}\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "typeorm",
            "model",
            "User",
            "users",
            {"entity": "User", "table": "users"},
        ),
        (
            "typeorm",
            "repository",
            "UserRepository",
            "User",
            {"model": "User", "repository": "Repository"},
        ),
    ]


def test_prisma_client_binding_extracts_repository():
    source = SourceFile(
        "src/repositories.ts",
        "import { PrismaClient } from '@prisma/client';\n"
        "const prisma = new PrismaClient();\n"
        "export const userRepository = prisma.user;\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "prisma",
            "repository",
            "userRepository",
            "user",
            {"client": "prisma", "model": "user"},
        )
    ]


def test_fastapi_middleware_decorator_extracts_boundary():
    source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.middleware('http')\n"
        "async def add_context(request, call_next):\n"
        "    return await call_next(request)\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "fastapi",
            "middleware",
            "add_context",
            "app",
            {"application": "app", "type": "http"},
        )
    ]


def test_flask_before_request_extracts_middleware_boundary():
    source = SourceFile(
        "app.py",
        "from flask import Flask\n"
        "app = Flask(__name__)\n"
        "@app.before_request\n"
        "def authenticate(): return None\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "flask",
            "middleware",
            "authenticate",
            "app",
            {"application": "app", "phase": "before_request"},
        )
    ]


def test_express_use_extracts_global_middleware_boundary():
    source = SourceFile(
        "app.js",
        "import express from 'express';\n"
        "const app = express();\n"
        "app.use(authenticate);\n",
    )

    entities = extract_framework_entities(source)

    assert [
        (entity.framework, entity.kind, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "express",
            "middleware",
            "authenticate",
            "app",
            {"handler": "authenticate", "owner": "app", "scope": "*"},
        )
    ]


def test_nestjs_use_guards_extracts_method_authorization_boundary():
    source = SourceFile(
        "admin.controller.ts",
        "import { Controller, Get, UseGuards } from '@nestjs/common';\n"
        "@Controller('admin')\n"
        "export class AdminController {\n"
        "  @Get()\n"
        "  @UseGuards(AuthGuard)\n"
        "  list() {}\n"
        "}\n",
    )

    entities = [
        entity
        for entity in extract_framework_entities(source)
        if entity.kind == "authorization"
    ]

    assert [
        (entity.framework, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "nestjs",
            "AdminController.list",
            "AuthGuard",
            {
                "controller": "AdminController",
                "guard": "AuthGuard",
                "handler": "AdminController.list",
                "route": "/admin",
            },
        )
    ]


def test_fastapi_depends_extracts_route_authorization_boundary():
    source = SourceFile(
        "admin.py",
        "from fastapi import Depends, FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/admin', dependencies=[Depends(require_admin)])\n"
        "def admin(): return None\n",
    )

    entities = [
        entity
        for entity in extract_framework_entities(source)
        if entity.kind == "authorization"
    ]

    assert [
        (entity.framework, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "fastapi",
            "GET /admin",
            "require_admin",
            {
                "dependency": "require_admin",
                "handler": "admin",
                "method": "GET",
                "route": "/admin",
            },
        )
    ]


def test_nestjs_use_interceptors_extracts_method_middleware_boundary():
    source = SourceFile(
        "users.controller.ts",
        "import { Controller, Get, UseInterceptors } from '@nestjs/common';\n"
        "@Controller('users')\n"
        "export class UsersController {\n"
        "  @Get()\n"
        "  @UseInterceptors(LoggingInterceptor)\n"
        "  list() {}\n"
        "}\n",
    )

    entities = [
        entity
        for entity in extract_framework_entities(source)
        if entity.kind == "middleware"
    ]

    assert [
        (entity.framework, entity.name, entity.target, dict(entity.attributes))
        for entity in entities
    ] == [
        (
            "nestjs",
            "UsersController.list",
            "LoggingInterceptor",
            {
                "controller": "UsersController",
                "handler": "UsersController.list",
                "interceptor": "LoggingInterceptor",
                "route": "/users",
            },
        )
    ]


def test_middleware_boundaries_fail_closed_on_dynamic_or_unsupported_metadata():
    fastapi_source = SourceFile(
        "api.py",
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.middleware('websocket')\n"
        "async def unsupported(request, call_next): return await call_next(request)\n",
    )
    express_source = SourceFile(
        "app.js",
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use(prefix, authenticate);\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(fastapi_source)
        if entity.kind == "middleware"
    ]
    assert not [
        entity
        for entity in extract_framework_entities(express_source)
        if entity.kind == "middleware"
    ]


def test_sqlalchemy_relative_lookalike_import_is_ignored():
    source = SourceFile(
        "models.py",
        "from .sqlalchemy.orm import DeclarativeBase\n"
        "class Base(DeclarativeBase): pass\n"
        "class Ghost(Base):\n"
        "    __tablename__ = 'ghosts'\n",
    )

    assert SQLAlchemyAnalyzer().extract_entities(source) == []


def test_celery_relative_lookalike_import_is_ignored():
    source = SourceFile(
        "tasks.py",
        "from .celery import Celery\n"
        "app = Celery('worker')\n"
        "@app.task()\n"
        "def ghost(): return None\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "celery"
    ]


def test_sqlalchemy_named_expression_rebinding_invalidates_constructor():
    source = SourceFile(
        "models.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        "(DeclarativeBase := fake)\n"
        "class Base(DeclarativeBase): pass\n"
        "class Ghost(Base):\n"
        "    __tablename__ = 'ghosts'\n",
    )

    assert SQLAlchemyAnalyzer().extract_entities(source) == []


def test_celery_named_expression_rebinding_invalidates_application():
    source = SourceFile(
        "tasks.py",
        "from celery import Celery\n"
        "app = Celery('worker')\n"
        "(app := fake)\n"
        "@app.task()\n"
        "def ghost(): return None\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "celery"
    ]


def test_react_parameter_shadowing_invalidates_imported_hook():
    source = SourceFile(
        "hooks.tsx",
        "import { useState } from 'react';\n"
        "export const useGhost = (useState) => useState();\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "react" and entity.kind == "hook"
    ]


def test_nextjs_default_exported_http_function_is_not_a_route_handler():
    source = SourceFile(
        "app/users/route.ts",
        "export default function GET() {}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs"
    ]


def test_nextjs_late_use_server_expression_is_not_a_directive():
    source = SourceFile(
        "app/actions.ts",
        "doThing();\n"
        "'use server';\n"
        "export async function ghost() {}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs" and entity.kind == "action"
    ]


def test_prisma_model_inside_block_comment_is_ignored():
    source = SourceFile(
        "schema.prisma",
        "/*\n"
        "model Ghost {\n"
        "  id Int @id\n"
        "}\n"
        "*/\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "prisma"
    ]


def test_angular_decorator_rebinding_invalidates_subsequent_component():
    source = SourceFile(
        "app.component.ts",
        "import { Component } from '@angular/core';\n"
        "Component = fake;\n"
        "@Component({ selector: 'app-root' })\n"
        "export class AppComponent {}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "angular"
    ]


def test_prisma_client_constructor_rebinding_invalidates_repository():
    source = SourceFile(
        "db.ts",
        "import { PrismaClient } from '@prisma/client';\n"
        "PrismaClient = fake;\n"
        "const prisma = new PrismaClient();\n"
        "export const userRepository = prisma.user;\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "prisma"
    ]


def test_typeorm_import_rebinding_invalidates_models_and_repositories():
    source = SourceFile(
        "entities.ts",
        "import { Entity, Repository } from 'typeorm';\n"
        "Entity = fake;\n"
        "Repository = fake;\n"
        "@Entity('users')\n"
        "export class User {}\n"
        "export class UserRepository extends Repository<User> {}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "typeorm"
    ]


def test_boundaries_command_returns_middleware_and_authorization_as_stable_json(
    tmp_path, monkeypatch
):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "app = FastAPI()\n"
        "@app.middleware('http')\n"
        "async def add_context(request, call_next): return await call_next(request)\n"
        "@app.get('/admin', dependencies=[Depends(require_admin)])\n"
        "def admin(): return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    result = subprocess.run(
        [sys.executable, "-m", "contextforge", "boundaries", "--format", "json"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0"
    assert payload["command"] == "boundaries"
    assert [entity["kind"] for entity in payload["boundaries"]] == [
        "middleware",
        "authorization",
    ]


def test_react_imported_hook_rebinding_invalidates_subsequent_hook():
    source = SourceFile(
        "hooks.tsx",
        "import { useState } from 'react';\n"
        "useState = fake;\n"
        "export const useGhost = () => useState();\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "react" and entity.kind == "hook"
    ]


def test_sqlalchemy_function_header_rebinding_invalidates_session_type():
    source = SourceFile(
        "repositories.py",
        "from sqlalchemy.orm import Session\n"
        "def mutate(value=(Session := object())): pass\n"
        "class Repo:\n"
        "    def __init__(self, session: Session): self.session = session\n",
    )

    assert SQLAlchemyAnalyzer().extract_entities(source) == []


def test_celery_function_header_rebinding_invalidates_application():
    source = SourceFile(
        "tasks.py",
        "from celery import Celery\n"
        "app = Celery('worker')\n"
        "@app.task()\n"
        "def job(value=(app := object())): return value\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "celery"
    ]


def test_celery_unresolved_decorator_expansion_is_ignored():
    source = SourceFile(
        "tasks.py",
        "from celery import shared_task\n"
        "opts = {'name': 'other'}\n"
        "@shared_task(name='known', **opts)\n"
        "def job(): return None\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "celery"
    ]


def test_prisma_syntactically_invalid_model_body_is_ignored():
    source = SourceFile(
        "schema.prisma",
        "model User {\n"
        "  this is not valid prisma !!!\n"
        "}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "prisma"
    ]


@pytest.mark.parametrize(
    "field_type, default",
    [
        ("Int", "notARealDefault"),
        ("Int", "notARealDefault()"),
        ("Int", '"wrong"'),
        ("Int", "true"),
        ("Int", "now()"),
        ("String", "autoincrement()"),
        ("Boolean", "1"),
        ("Int", "[1, 2]"),
        ("Int", "1, 2"),
    ],
)
def test_prisma_semantically_invalid_defaults_reject_the_whole_schema(
    field_type, default
):
    source = SourceFile(
        "schema.prisma",
        "model User {\n"
        "  id Int @id\n"
        f"  value {field_type} @default({default})\n"
        "}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "prisma"
    ]


@pytest.mark.parametrize(
    "field_type, default",
    [
        ("Int", "-1"),
        ("Float", "1.5e2"),
        ("Boolean", "true"),
        ("String", '"known"'),
        ("DateTime", '"2020-03-19T14:21:00+02:00"'),
        ("Int", "autoincrement()"),
        ("DateTime", "now()"),
        ("String", "uuid()"),
    ],
)
def test_prisma_proven_scalar_defaults_are_accepted(field_type, default):
    entities = extract_framework_entities(
        SourceFile(
            "schema.prisma",
            "model User {\n"
            "  id Int @id\n"
            f"  value {field_type} @default({default})\n"
            "}\n",
        )
    )

    assert [entity.name for entity in entities if entity.framework == "prisma"] == [
        "User"
    ]


def test_nextjs_route_export_rebinding_is_ignored():
    source = SourceFile(
        "app/x/route.ts",
        "export function GET() {}\n"
        "GET = fake;\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs"
    ]


def test_nextjs_server_action_export_rebinding_is_ignored():
    source = SourceFile(
        "app/actions.ts",
        '"use server";\n'
        "export function save() {}\n"
        "save = fake;\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs"
    ]


def test_nextjs_duplicate_named_route_exports_are_ignored():
    source = SourceFile(
        "app/x/route.ts",
        "export function GET() {}\n"
        "export function GET() {}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs"
    ]


def test_framework_entity_query_uses_total_projected_order(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "api.py").write_text(
        "from fastapi import Depends, FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/admin', dependencies=[Depends(zeta), Depends(alpha)])\n"
        "def admin(): return None\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONTEXTFORGE_CACHE_DIR", str(tmp_path / "cache"))

    entities = find_framework_entities(repository, kinds=("authorization",))

    assert [entity["target"] for entity in entities] == ["alpha", "zeta"]


@pytest.mark.parametrize(
    "framework, source",
    [
        (
            "sqlalchemy",
            "from sqlalchemy.orm import Session\n"
            "class Mutate:\n"
            "    global Session\n"
            "    Session = object()\n"
            "class Repo:\n"
            "    def __init__(self, session: Session): self.session = session\n",
        ),
        (
            "celery",
            "from celery import Celery\n"
            "app = Celery('worker')\n"
            "class Mutate:\n"
            "    global app\n"
            "    app = object()\n"
            "@app.task()\n"
            "def job(): return None\n",
        ),
    ],
)
def test_python_framework_class_global_rebinding_is_ignored(framework, source):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("module.py", source))
        if entity.framework == framework
    ]


@pytest.mark.parametrize(
    "framework, source",
    [
        (
            "sqlalchemy",
            "from sqlalchemy.orm import Session\n"
            "def mutate():\n"
            "    global Session\n"
            "    Session = object()\n"
            "mutate()\n"
            "class Repo:\n"
            "    def __init__(self, session: Session): self.session = session\n",
        ),
        (
            "celery",
            "from celery import Celery\n"
            "app = Celery('worker')\n"
            "def mutate():\n"
            "    global app\n"
            "    app = object()\n"
            "mutate()\n"
            "@app.task()\n"
            "def job(): return None\n",
        ),
    ],
)
def test_python_framework_called_global_rebinding_is_ignored(framework, source):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("module.py", source))
        if entity.framework == framework
    ]


def test_sqlalchemy_declarative_base_expansion_is_ignored():
    source = SourceFile(
        "models.py",
        "from sqlalchemy.orm import declarative_base\n"
        "Base = declarative_base(*range(100))\n"
        "class User(Base):\n"
        "    __tablename__ = 'users'\n",
    )

    assert SQLAlchemyAnalyzer().extract_entities(source) == []


def test_celery_constructor_expansion_is_ignored():
    source = SourceFile(
        "tasks.py",
        "from celery import Celery\n"
        "app = Celery(*range(100))\n"
        "@app.task()\n"
        "def job(): return None\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "celery"
    ]


@pytest.mark.parametrize(
    "body",
    [
        "  id Int @default(!!!)\n",
        "  id Int @default()\n",
        "  id Int @db\n",
        "  id String @db.VarChar(!!!)\n",
        "  id Int @id\n  @@index([!!!])\n",
        "  id Int @id\n  @@index([])\n",
    ],
)
def test_prisma_invalid_attribute_syntax_is_ignored(body):
    source = SourceFile("schema.prisma", f"model User {{\n{body}}}\n")

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "prisma"
    ]


@pytest.mark.parametrize(
    "sibling",
    [
        "const GET = () => {};",
        "function GET() {}",
        "class GET {}",
    ],
)
def test_nextjs_nonexport_duplicate_route_binding_is_ignored(sibling):
    source = SourceFile(
        "app/x/route.ts",
        f"export function GET() {{}}\n{sibling}\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "nextjs"
    ]


@pytest.mark.parametrize(
    "framework, source",
    [
        (
            "sqlalchemy",
            "from sqlalchemy.orm import Session\n"
            "def mutate():\n"
            "    global Session\n"
            "    Session = object()\n"
            "class Execute:\n"
            "    mutate()\n"
            "class Repo:\n"
            "    def __init__(self, session: Session): self.session = session\n",
        ),
        (
            "celery",
            "from celery import Celery\n"
            "app = Celery('worker')\n"
            "def mutate():\n"
            "    global app\n"
            "    app = object()\n"
            "class Execute:\n"
            "    mutate()\n"
            "@app.task()\n"
            "def job(): return None\n",
        ),
    ],
)
def test_python_framework_class_body_call_effect_is_applied(framework, source):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("module.py", source))
        if entity.framework == framework
    ]


@pytest.mark.parametrize(
    "framework, prefix, trusted_name, suffix",
    [
        (
            "sqlalchemy",
            "from sqlalchemy.orm import Session\n",
            "Session",
            "class Repo:\n"
            "    def __init__(self, session: Session): self.session = session\n",
        ),
        (
            "celery",
            "from celery import Celery\napp = Celery('worker')\n",
            "app",
            "@app.task()\ndef job(): return None\n",
        ),
    ],
)
def test_python_framework_nested_mutator_effect_is_applied(
    framework, prefix, trusted_name, suffix
):
    source = (
        prefix
        + "def inner():\n"
        + f"    global {trusted_name}\n"
        + f"    {trusted_name} = object()\n"
        + "def middle():\n"
        + "    inner()\n"
        + "def outer():\n"
        + "    middle()\n"
        + "outer()\n"
        + suffix
    )

    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("module.py", source))
        if entity.framework == framework
    ]


@pytest.mark.parametrize(
    "framework, prefix, trusted_name, suffix",
    [
        (
            "sqlalchemy",
            "from sqlalchemy.orm import Session\n",
            "Session",
            "class Repo:\n"
            "    def __init__(self, session: Session): self.session = session\n",
        ),
        (
            "celery",
            "from celery import Celery\napp = Celery('worker')\n",
            "app",
            "@app.task()\ndef job(): return None\n",
        ),
    ],
)
def test_python_framework_aliased_mutator_effect_is_applied(
    framework, prefix, trusted_name, suffix
):
    source = (
        prefix
        + "def mutate():\n"
        + f"    global {trusted_name}\n"
        + f"    {trusted_name} = object()\n"
        + "run = mutate\n"
        + "run()\n"
        + suffix
    )

    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("module.py", source))
        if entity.framework == framework
    ]


@pytest.mark.parametrize(
    "alias_source",
    [
        "run: object = mutate\n",
        "other = run = mutate\n",
        "(run := mutate)\n",
        "run, = (mutate,)\n",
        "run = mutate if enabled else harmless\n",
    ],
)
@pytest.mark.parametrize(
    "framework, prefix, trusted_name, suffix",
    [
        ("sqlalchemy", "from sqlalchemy.orm import Session\n", "Session", "class Repo:\n    def __init__(self, session: Session): self.session = session\n"),
        ("celery", "from celery import Celery\napp = Celery('worker')\n", "app", "@app.task()\ndef job(): return None\n"),
    ],
)
def test_python_framework_supported_alias_shapes_apply_mutator_effects(
    alias_source, framework, prefix, trusted_name, suffix
):
    source = (prefix + "enabled = True\ndef harmless(): pass\ndef mutate():\n"
              + f"    global {trusted_name}\n    {trusted_name} = object()\n"
              + alias_source + "run()\n" + suffix)
    assert not [entity for entity in extract_framework_entities(SourceFile("module.py", source)) if entity.framework == framework]


@pytest.mark.parametrize(
    "framework, prefix, trusted_name, suffix",
    [
        ("sqlalchemy", "from sqlalchemy.orm import Session\n", "Session", "class Repo:\n    def __init__(self, session: Session): self.session = session\n"),
        ("celery", "from celery import Celery\napp = Celery('worker')\n", "app", "@app.task()\ndef job(): return None\n"),
    ],
)
def test_python_framework_function_effects_follow_definition_timing(framework, prefix, trusted_name, suffix):
    source = (prefix + "def mutate():\n" + f"    global {trusted_name}\n    {trusted_name} = object()\n"
              + "mutate()\ndef mutate(): pass\n" + suffix)
    assert not [entity for entity in extract_framework_entities(SourceFile("module.py", source)) if entity.framework == framework]


@pytest.mark.parametrize(
    "framework, prefix, trusted_name, suffix",
    [
        ("sqlalchemy", "from sqlalchemy.orm import Session\n", "Session", "class Repo:\n    def __init__(self, session: Session): self.session = session\n"),
        ("celery", "from celery import Celery\napp = Celery('worker')\n", "app", "@app.task()\ndef job(): return None\n"),
    ],
)
def test_python_framework_call_before_definition_invalidates_later_evidence(framework, prefix, trusted_name, suffix):
    source = (prefix + "late()\ndef late():\n" + f"    global {trusted_name}\n    {trusted_name} = object()\n" + suffix)
    assert not [entity for entity in extract_framework_entities(SourceFile("module.py", source)) if entity.framework == framework]


@pytest.mark.parametrize(
    "body",
    [
        "def mutate():\n    global {trusted}\n    {trusted} = object()\nrun = lambda: mutate()\nrun()\n",
        "def outer():\n    def inner():\n        global {trusted}\n        {trusted} = object()\n    inner()\nouter()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\ndef outer():\n    run = mutate\n    run()\nouter()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\nrun = mutate\ndef unrelated(): pass\nrun()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\nrun = mutate\ndef mutate(): pass\nrun()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\nrun = mutate if enabled else unknown\nrun()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\nitems = [(run := mutate)]\nrun()\n",
        "def mutate():\n    global {trusted}\n    {trusted} = object()\n(run := mutate)()\n",
        "def decorate(fn):\n    def replacement():\n        global {trusted}\n        {trusted} = object()\n    return replacement\n@decorate\ndef harmless(): pass\nharmless()\n",
    ],
)
@pytest.mark.parametrize(
    "framework, prefix, trusted, suffix",
    [
        ("sqlalchemy", "from sqlalchemy.orm import Session\nenabled = True\n", "Session", "class Repo:\n    def __init__(self, session: Session): self.session = session\n"),
        ("celery", "from celery import Celery\napp = Celery('worker')\nenabled = True\n", "app", "@app.task()\ndef job(): return None\n"),
    ],
)
def test_python_framework_complex_callable_mutations_fail_closed(
    body, framework, prefix, trusted, suffix
):
    source = prefix + body.format(trusted=trusted) + suffix
    assert not [entity for entity in extract_framework_entities(SourceFile("module.py", source)) if entity.framework == framework]


@pytest.mark.parametrize(
    "body",
    [
        "late()\nlate = lambda: None\n",
        "def wrapper(value=late()): pass\ndef late(): pass\n",
        "@late\ndef wrapper(): pass\ndef late(fn): return fn\n",
        "class Wrapper(late()): pass\ndef late(): return object\n",
        "(lambda: late())()\ndef late(): pass\n",
        "def outer():\n    inner()\n    def inner(): pass\nouter()\n",
    ],
)
@pytest.mark.parametrize(
    "framework, prefix, suffix",
    [
        ("sqlalchemy", "from sqlalchemy.orm import Session\n", "class Repo:\n    def __init__(self, session: Session): self.session = session\n"),
        ("celery", "from celery import Celery\napp = Celery('worker')\n", "@app.task()\ndef job(): return None\n"),
    ],
)
def test_python_framework_unreachable_later_evidence_is_ignored(
    body, framework, prefix, suffix
):
    source = prefix + body + suffix
    assert not [entity for entity in extract_framework_entities(SourceFile("module.py", source)) if entity.framework == framework]


@pytest.mark.parametrize(
    "base_declaration",
    [
        "class Base(DeclarativeBase, DeclarativeBase): pass\n",
        "class Base(DeclarativeBase, unknown=True): pass\n",
    ],
)
def test_sqlalchemy_invalid_declarative_base_class_shape_is_ignored(
    base_declaration,
):
    source = SourceFile(
        "models.py",
        "from sqlalchemy.orm import DeclarativeBase\n"
        + base_declaration
        + "class User(Base):\n"
        + "    __tablename__ = 'users'\n",
    )

    assert not [
        entity
        for entity in extract_framework_entities(source)
        if entity.framework == "sqlalchemy" and entity.kind == "model"
    ]


@pytest.mark.parametrize(
    "schema",
    [
        "model User {\n  id Int @id\n  id Int @unique\n}\n",
        "model User {\n  id Int @id\n  value Mystery\n}\n",
        "model User {\n  id Int @id(foo)\n}\n",
        "model User {\n  name String\n}\n",
        "model User {\n  id Int @id\n  posts Post[]\n}\n"
        "model Post {\n  id Int @id\n  user User @relation(fields: [id], references: [missing])\n}\n",
        "model User {\n  id Int @id\n}\nthis is invalid\n",
        "model User {\n  id Int @id\n}\n/* unterminated\n",
        "/* outer /* nested */ model Ghost {\n  id Int @id\n}\n*/\n",
    ],
)
def test_prisma_whole_schema_must_be_conservatively_valid(schema):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("schema.prisma", schema))
        if entity.framework == "prisma"
    ]


@pytest.mark.parametrize(
    "schema",
    [
        "model User {\n  id Int\n  @@id([missing])\n}\n",
        "model User {\n  id Int\n  @@id([id, id])\n}\n",
        "model User {\n  id Int @id\n  other Int @id\n}\n",
        "model User {\n  id Int\n  @@unique([missing])\n}\n",
        'model User {\n  id Int @id\n  @@map("shared")\n}\nmodel Team {\n  id Int @id\n  @@map("shared")\n}\n',
        'model User {\n  id Int @id\n  @@schema("tenant")\n}\n',
        'model User {\n  id Int @id @map("shared")\n  other Int @map("shared")\n}\n',
        'model User {\n  id Int @id @map("other")\n  other Int\n}\n',
    ],
)
def test_prisma_constraint_semantics_must_be_valid(schema):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("schema.prisma", schema))
        if entity.framework == "prisma"
    ]


@pytest.mark.parametrize(
    "module",
    [
        "export function GET() {}\nexport { GET };\n",
        "import { GET } from './other';\nexport function GET() {}\n",
        "import { other as GET } from './other';\nexport function GET() {}\n",
        "import GET from './other';\nexport function GET() {}\n",
        "import * as GET from './other';\nexport function GET() {}\n",
        "export { other as GET } from './other';\nexport function GET() {}\n",
        "export * as GET from './other';\nexport function GET() {}\n",
    ],
)
def test_nextjs_route_module_name_collisions_are_ignored(module):
    assert not [
        entity
        for entity in extract_framework_entities(
            SourceFile("app/x/route.js", module)
        )
        if entity.framework == "nextjs"
    ]


@pytest.mark.parametrize(
    "module",
    [
        '"use server";\nexport async function save() {}\nexport { save };\n',
        '"use client";\n"use server";\nexport async function save() {}\n',
        '"use server";\n"use client";\nexport async function save() {}\n',
    ],
)
def test_nextjs_server_action_module_conflicts_are_ignored(module):
    assert not [
        entity
        for entity in extract_framework_entities(
            SourceFile("app/actions.js", module)
        )
        if entity.framework == "nextjs"
    ]


@pytest.mark.parametrize(
    "prefix",
    [
        "import { other } from './other';\n",
        "export { other } from './other';\n",
        "export * from './other';\n",
    ],
)
def test_nextjs_unrelated_module_bindings_preserve_route(prefix):
    entities = extract_framework_entities(
        SourceFile("app/x/route.js", prefix + "export function GET() {}\n")
    )

    assert [
        (entity.kind, entity.name)
        for entity in entities
        if entity.framework == "nextjs"
    ] == [("route", "GET /x")]


@pytest.mark.parametrize(
    "module",
    [
        "export function GET() { await load(); }\n",
        "export function GET(value, value) {}\n",
        "export function GET() { return { __proto__: null, __proto__: {} }; }\n",
        "const await = 1; export function GET() {}\n",
    ],
)
def test_nextjs_ecmascript_early_errors_are_ignored(module):
    assert not [
        entity
        for entity in extract_framework_entities(SourceFile("app/x/route.js", module))
        if entity.framework == "nextjs"
    ]


@pytest.mark.parametrize(
    "module",
    [
        'import express from "express"; const app = express(); '
        'function handler(req, res) {} app.get("/x", handler); const n = 010;\n',
        'import express from "express"; const app = express(); '
        'function handler(req, res) {} app.get("/x", handler); delete handler;\n',
        'import express from "express"; const app = express(); '
        'function handler(req, res) {} app.get("/x", handler); with ({}) {}\n',
    ],
)
def test_javascript_strict_mode_early_errors_are_ignored(module):
    assert not extract_framework_entities(SourceFile("app.js", module))


@pytest.mark.parametrize(
    "module",
    [
        "await load(); export function GET() {}\n",
        "export async function GET() { await load(); }\n",
        "export function GET(first, second) {}\n",
        "const __proto__ = null; export function GET() { return { __proto__ }; }\n",
        "export function GET() { return { ['__proto__']: null, __proto__() {} }; }\n",
    ],
)
def test_nextjs_ecmascript_early_error_positive_controls(module):
    assert [
        (entity.kind, entity.name)
        for entity in extract_framework_entities(SourceFile("app/x/route.js", module))
        if entity.framework == "nextjs"
    ] == [("route", "GET /x")]


@pytest.mark.parametrize(
    "path, framework, module",
    [
        (
            "server.mjs",
            "express",
            "import express from 'express';\n"
            "function invalid() { await load(); }\n"
            "const app = express(); app.get('/x', handler);\n",
        ),
        (
            "controller.ts",
            "nestjs",
            "import { Controller, Get } from '@nestjs/common';\n"
            "function invalid() { await load(); }\n"
            "@Controller('x') class X { @Get() get() {} }\n",
        ),
        (
            "component.jsx",
            "react",
            "import React from 'react';\n"
            "function invalid() { await load(); }\n"
            "export function Widget() { return <div />; }\n",
        ),
        (
            "component.ts",
            "angular",
            "import { Component } from '@angular/core';\n"
            "function invalid() { await load(); }\n"
            "@Component({selector: 'x-widget'}) export class Widget {}\n",
        ),
        (
            "repository.ts",
            "prisma",
            "import { PrismaClient } from '@prisma/client';\n"
            "function invalid() { await load(); }\n"
            "const prisma = new PrismaClient(); export const users = prisma.user;\n",
        ),
        (
            "entity.ts",
            "typeorm",
            "import { Entity } from 'typeorm';\n"
            "function invalid() { await load(); }\n"
            "@Entity('users') export class User {}\n",
        ),
        (
            "server.cjs",
            "express",
            "import express from 'express';\n"
            "const app = express(); app.get('/x', handler);\n",
        ),
    ],
)
def test_javascript_frameworks_require_a_valid_whole_module(path, framework, module):
    entities = extract_framework_entities(SourceFile(path, module))
    assert not [entity for entity in entities if entity.framework == framework]


@pytest.mark.parametrize(
    "module",
    [
        "export function GET() {}\nbreak;\n",
        "export function GET() {}\nreturn;\n",
        "export function GET() {}\nexport { missing };\n",
    ],
)
def test_nextjs_remaining_module_early_errors_are_ignored(module):
    assert not [
        entity
        for entity in extract_framework_entities(
            SourceFile("app/x/route.js", module)
        )
        if entity.framework == "nextjs"
    ]
