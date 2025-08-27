import sys
import json
import csv

old_json = sys.argv[1]
new_json = sys.argv[2]
compare = sys.argv[3]

new_fail = []
old_fail = []
shared_fail = []

f = open(old_json)
old_data = json.load(f)
f.close()

for test in old_data['tests']:
    if test['data']['status'] == 'failure':
        old_fail.append({"id": test['data']['tool_id'], "version": test['data']['tool_version'], "test_number": test['data']['test_index'], "status": "fail"})
    elif test['data']['status'] == 'error':
        old_fail.append({"id": test['data']['tool_id'], "version": test['data']['tool_version'], "test_number": test['data']['test_index'], "status": "error"})

f = open(new_json)
new_data = json.load(f)
f.close()

for test in new_data['tests']:
    if test['data']['status'] == 'failure':
        new_fail.append({"id": test['data']['tool_id'], "version": test['data']['tool_version'], "test_number": test['data']['test_index'], "status": "fail"})
    elif test['data']['status'] == 'error':
        new_fail.append({"id": test['data']['tool_id'], "version": test['data']['tool_version'], "test_number": test['data']['test_index'], "status": "error"})

for test in new_fail:
    if test in old_fail:
        shared_fail.append(test)

#structure of output file: {tool id, tool test number, number of weeks in a row this has failed}
export_compare = []
with open(compare, 'r') as file:
    for test in shared_fail:
        for line in file:
            line = line.strip().split()
            if test['id'] == line['tool_id'] and test['test_number'] == line['test_number']:
                test_num = line['test_number']+1
                export_compare.append({"tool_id": test['id'], "test_number": test['test_number'], "weeks": test_num})
                break    
        export_compare.append({"tool_id": test['id'], "test_number": test['test_number'], "weeks": 2})

with open("summary_results.json", 'w', newline="") as output:
    tsv_writer = csv.writer(output, delimiter="\t")
    for test in export_compare:
        tsv_writer.write(test)