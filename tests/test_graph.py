import os
import tempfile
import shutil
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from vexindex.config import settings

# Setup a temporary DB for tests
original_db_path = settings.VEXINDEX_DB_PATH
temp_dir = tempfile.mkdtemp()
test_db_path = os.path.join(temp_dir, "test_graph.db")
settings.VEXINDEX_DB_PATH = test_db_path

from vexindex.main import app
from vexindex.db import init_db
from vexindex.mcp import call_tool

@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    settings.VEXINDEX_DB_PATH = original_db_path
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.mark.asyncio
async def test_graph_resolution_and_api():
    # 1. Create a temporary project directory
    with tempfile.TemporaryDirectory() as project_dir:
        # Write Python file
        py_file = os.path.join(project_dir, "math_utils.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write(
                "import math\n\n"
                "class BaseEngine:\n"
                "    pass\n\n"
                "class MathEngine(BaseEngine):\n"
                "    def compute(self, x):\n"
                "        return math.sin(x)\n\n"
                "def run_compute():\n"
                "        engine = MathEngine()\n"
                "        engine.compute(1.0)\n"
            )
            
        # Write Javascript file
        js_file = os.path.join(project_dir, "app.js")
        with open(js_file, "w", encoding="utf-8") as f:
            f.write(
                "import { sin } from 'math-lib';\n\n"
                "class App {\n"
                "    start() {\n"
                "        const result = sin(1.0);\n"
                "        console.log(result);\n"
                "    }\n"
                "}\n"
            )
            
        # Mock vector store embed/upsert to bypass Ollama and Qdrant in tests
        mock_embed = patch("vexindex.vector.vector_store.embed_text", return_value=[0.1]*768)
        mock_upsert = patch("vexindex.vector.vector_store.upsert_chunks", return_value=None)
        mock_delete = patch("vexindex.vector.vector_store.delete_chunks_for_file", return_value=None)
        
        with mock_embed, mock_upsert, mock_delete:
            with TestClient(app) as client:
                # 2. Register project (runs index_project synchronously via TestClient's BackgroundTasks execution)
                response = client.post(
                    "/projects",
                    json={"name": "Graph Test Project", "root_path": project_dir}
                )
                assert response.status_code == 201
                project_id = response.json()["id"]
                
                # Verify index status becomes idle
                status_res = client.get(f"/index/status/{project_id}")
                assert status_res.status_code == 200
                assert status_res.json()["status"] == "idle"
                
                # 3. Test REST endpoints: /graph/definitions
                # Python class definition lookup
                def_res = client.get(f"/graph/definitions?name=MathEngine&project_id={project_id}")
                assert def_res.status_code == 200
                defs = def_res.json()
                assert len(defs) == 1
                assert defs[0]["name"] == "MathEngine"
                assert defs[0]["kind"] == "class"
                assert defs[0]["file_path"] == py_file
                assert defs[0]["chunk_id"] is not None
                
                # Python method definition lookup
                def_res_2 = client.get(f"/graph/definitions?name=MathEngine.compute&project_id={project_id}")
                assert def_res_2.status_code == 200
                defs_2 = def_res_2.json()
                assert len(defs_2) == 1
                assert defs_2[0]["name"] == "compute"
                assert defs_2[0]["kind"] == "method"
                assert defs_2[0]["scope"] == "MathEngine"
                
                # JS class definition lookup
                def_res_3 = client.get(f"/graph/definitions?name=App&project_id={project_id}")
                assert def_res_3.status_code == 200
                defs_3 = def_res_3.json()
                assert len(defs_3) == 1
                assert defs_3[0]["name"] == "App"
                assert defs_3[0]["file_path"] == js_file
                
                # 4. Test REST endpoints: /graph/callers & /graph/callees
                # MathEngine.compute calls math.sin
                callees_res = client.get(f"/graph/callees?name=MathEngine.compute&project_id={project_id}")
                assert callees_res.status_code == 200
                callees = callees_res.json()
                assert any(c["target_symbol"] == "math.sin" for c in callees)
                
                # run_compute calls MathEngine and engine.compute
                callees_res_2 = client.get(f"/graph/callees?name=run_compute&project_id={project_id}")
                assert callees_res_2.status_code == 200
                callees_2 = callees_res_2.json()
                assert any(c["target_symbol"] == "MathEngine" for c in callees_2)
                assert any(c["target_symbol"] == "engine.compute" for c in callees_2)
                
                # App.start calls sin and console.log
                callees_res_3 = client.get(f"/graph/callees?name=App.start&project_id={project_id}")
                assert callees_res_3.status_code == 200
                callees_3 = callees_res_3.json()
                assert any(c["target_symbol"] == "sin" for c in callees_3)
                assert any(c["target_symbol"] == "console.log" for c in callees_3)
                
                # 5. Test REST endpoints: /graph/relations (INHERITS / IMPORTS)
                rel_res = client.get(f"/graph/relations?name=MathEngine&project_id={project_id}")
                assert rel_res.status_code == 200
                rels = rel_res.json()
                assert any(r["relation_type"] == "INHERITS" and r["target_symbol"] == "BaseEngine" for r in rels)
                
                # App imports sin
                rel_res_2 = client.get(f"/graph/relations?name=sin&project_id={project_id}")
                assert rel_res_2.status_code == 200
                rels_2 = rel_res_2.json()
                assert any(r["relation_type"] == "IMPORTS" and r["target_symbol"] == "math-lib.sin" for r in rels_2)

                # 6. Test MCP Server tool calls
                # Tool: find_symbol_definition
                mcp_def = await call_tool("find_symbol_definition", {"name": "MathEngine", "project_id": project_id})
                assert len(mcp_def) == 1
                assert "math_utils.py" in mcp_def[0].text
                assert "class MathEngine(BaseEngine)" in mcp_def[0].text
                
                # Tool: get_symbol_relations
                mcp_rel = await call_tool("get_symbol_relations", {"name": "MathEngine", "project_id": project_id})
                assert len(mcp_rel) == 1
                assert "INHERITS" in mcp_rel[0].text
                assert "BaseEngine" in mcp_rel[0].text
                
                # Tool: find_callers (find callers of MathEngine)
                mcp_callers = await call_tool("find_callers", {"name": "MathEngine", "project_id": project_id})
                assert len(mcp_callers) == 1
                assert "run_compute" in mcp_callers[0].text
                
                # Clean up project
                del_res = client.delete(f"/projects/{project_id}")
                assert del_res.status_code == 200
