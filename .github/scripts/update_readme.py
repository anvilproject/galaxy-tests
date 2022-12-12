from datetime import datetime
import json
import sys
import yaml

from jinja2 import Template

args = sys.argv
inchunk = args[1]
allchunks = args[2]
readme_path = args[3]

with open(inchunk, 'r') as f:
    new_chunk = json.load(f)
    chunk_id = list(new_chunk.keys())[0]
    new_chunk = new_chunk.get(chunk_id)

with open(allchunks, 'r+') as f:
    chunks = json.load(f)
    old_chunk = chunks.get(chunk_id, {})
    new_chunk['run2'] = old_chunk.get('run1', 'N/A')
    new_chunk['date2'] = old_chunk.get('date1', 'N/A')
    chunks[chunk_id] = new_chunk
    f.seek(0)
    f.write(json.dumps(chunks, indent=4))
    f.truncate()

with open(".github/templates/README.md.j2", "r") as f:
    template = Template(f.read())

with open(readme_path, "w") as f:
    ids = list(chunks.keys())
    ids = [int(x) for x in ids]
    ids.sort()
    ids = [str(x) for x in ids]
    # Write the chunked test reports table
    reports_html = "<thead><tr><th>Report ID</th><th>Tool List</th><th>Latest report</th><th>Previous report</th></tr></thead><tbody>"
    for eachid in ids:
        eachchunk = chunks.get(eachid)
        reports_html += f"""
    <tr>
        <td>{eachid}</td>
        <td><a href=\"{eachchunk.get("tools")}\">Toolset</a></td>
        <td><a href=\"{eachchunk.get("run1")}\">{eachchunk.get("date1")}</a></td>
        <td><a href=\"{eachchunk.get("run2")}\">{eachchunk.get("date2")}</a></td>
    </tr>"""
    reports_html += "</tbody>"
    # List tools individually
    tool_tests_html = "<thead<tr><th>#</th><th>Report ID</th><th>Tool ID</th><th>Test #</th><th>Test results</th></thead><tbody>"
    count = 0
    for each_id in ids:
        chunk = chunks.get(each_id)
        chunk_tools_url = chunk.get('tools')
        chunk_tools_path = chunk_tools_url.replace('https://github.com/anvilproject/galaxy-tests/blob/main/', '')
        with open(chunk_tools_path, 'r') as tf:
            chunk_tools = yaml.safe_load(tf)
            for tool in chunk_tools['tools']:
                for rev in tool['revisions']:
                    count += 1
                    tool_tests_html += f"""
    <tr>
        <td>{count}</td>
        <td><a href=\"{chunk.get("run1")}\">{each_id}</a></td>
        <td>{tool['owner']}/{tool['name']}@{rev}</td>"""
                    test_count = 0
                    for test in range(3):  # TODO: Link with tests
                        test_count += 1
                        if test_count == 1:
                            tool_tests_html += f"""
        <td>Test #{test_count}</td>
        <td><span style='color:red'>F</span>P</td>"""
                        else:
                            tool_tests_html += f"""
    <tr>
        <td></td><td></td><td></td>
        <td>Test #{test_count}</td>
        <td><span style='color:red'>F</span>P</td>
    </tr>"""
                    tool_tests_html +="</tr>"
    tool_tests_html += "</tbody>"

    f.write(template.render(toolreports=reports_html, tooltests=tool_tests_html, reportdir=readme_path.replace("/README.md", "")))
