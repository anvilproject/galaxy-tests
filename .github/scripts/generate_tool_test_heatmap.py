import os
import re
import json
from collections import defaultdict
from collections import defaultdict
from unittest import result

# Define the base directory
BASE_DIR = "../../reports/anvil-production/tool-tests"
README_FILE = "../../reports/anvil-production/README.md"

def get_results_files(total_files_max):
    """
    Scans the 'reports/anvil-production/tool-tests' directory for folders
    with names containing the datetime format 'YYYY-MM-DD-HH-MM-SS' at the first level.
    Outputs the list of matching folder paths to the console log, limited to 'total_files_max' results.
    The directory list is sorted in descending order before selecting the first 'total_files_max' results.
    
    Parameters:
        total_files_max (int): The maximum number of matching folder paths to return.
    """
    datetime_pattern = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}")
    matching_folders = []
    
    if not os.path.exists(BASE_DIR):
        print(f"Directory '{BASE_DIR}' does not exist.")
        return matching_folders

    sorted_entries = sorted(os.listdir(BASE_DIR), reverse=True)

    for entry in sorted_entries:
        entry_path = os.path.join(BASE_DIR, entry)
        if os.path.isdir(entry_path) and datetime_pattern.search(entry):
            matching_folders.append(f"{entry_path}/results.json")
        if len(matching_folders) >= total_files_max:
            break
    
    return matching_folders

def join_results(total_files_max):
    """
    Calls get_results_files() to retrieve result file paths, then processes each file
    to extract and print the specified parameters. Outputs the combined results to a JSON file.
    
    Parameters:
        total_files_max (int): The maximum number of result files to process.
    """
    result_files = get_results_files(total_files_max)
    combined_results = []

    for result_file in result_files:
        if not os.path.exists(result_file):
            print(f"File not found: {result_file}")
            continue

        try:
            with open(result_file, 'r') as file:
                data = json.load(file)
                
                # Check if "tests" exists and is a list
                if "tests" in data and isinstance(data["tests"], list):
                    for test_data in data["tests"]:  # Iterate over all objects in the "tests" array

                        # Extract the error parameters
                        output_problems = _clean_output(test_data.get("data", {}).get("output_problems", ""))
                        execution_problem = _clean_output(test_data.get("data", {}).get("execution_problem", ""))
                        problem_short = output_problems + execution_problem
                        problem_short = _clean_output(problem_short)

                        result_data = {
                            "file": result_file,
                            "id": test_data.get("id"),
                            "data": {
                                "status": test_data.get("data", {}).get("status", "Unknown"),
                                "tool_id": test_data.get("data", {}).get("tool_id", "Unknown"),
                                "tool_version": test_data.get("data", {}).get("tool_version", "Unknown"),
                                "test_index": test_data.get("data", {}).get("test_index", "Unknown"),
                                "time_seconds": test_data.get("data", {}).get("time_seconds", 0),
                                "problem_short": problem_short,
                            },
                        }
                        combined_results.append(result_data)
                    
                        # Print the full path and extracted parameters for each test
                        # print(f"File: {result_file}")
                        # print(result_data)
                else:
                    print(f"No valid 'tests' data found in file: {result_file}")
        except Exception as e:
            print(f"Error processing file {result_file}: {e}")
    
    return combined_results

def aggregate_results(total_files_max):
    """
    Calls the join_results() method to retrieve joined results, then aggregates the data
    based on the "id" value to produce consolidated results.

    Parameters:
        total_files_max (int): The maximum number of result files to process.

    Returns:
        aggregated_results (list): A list of aggregated results grouped by "id".
    """
    # Call join_results() to get the joined results
    joined_results = join_results(total_files_max)

    # Dictionary to hold aggregated data
    aggregation = defaultdict(lambda: {
        "file": [],
        "data": {
            "tool_id": None,
            "tool_version": None,
            "test_index": 0,
            "test_index_calc": "count",
            "time_seconds_item": [],
            "time_seconds": 0,
            "time_calc": "total",
            "status_item": [],
            "status": 0.0,
            "status_calc": "average",
            "problem_short": ""
        }
    })

    # Process each result in joined_results
    for result in joined_results:
        result_id = result["id"]
        result_file = result["file"]
        result_data = result["data"]

        # Aggregate "file" values into a list
        if result_file not in aggregation[result_id]["file"]:
            aggregation[result_id]["file"].append(result_file)

        # Set "tool_id" and "tool_version" (should already be unique)
        aggregation[result_id]["data"]["tool_id"] = result_data["tool_id"]
        aggregation[result_id]["data"]["tool_version"] = result_data["tool_version"]

        # Increment "test_index" as a count of consolidated objects
        aggregation[result_id]["data"]["test_index"] += 1

        # Add "time_seconds" to the total and append to "time_seconds_item"
        aggregation[result_id]["data"]["time_seconds"] += result_data["time_seconds"]
        aggregation[result_id]["data"]["time_seconds_item"].append(result_data["time_seconds"])

        # Calculate "status" as an average (fail=0, success=1) and append to "status_item"
        status_value = 1 if result_data["status"] == "success" else 0
        aggregation[result_id]["data"]["status"] += status_value
        aggregation[result_id]["data"]["status_item"].append(status_value)
        
        # Store the output problems
        # aggregation[result_id]["data"]["problem_short"] += result_data["problem_short"]
        if not aggregation[result_id]["data"]["problem_short"] and len(result_data["problem_short"]) > 0:
            aggregation[result_id]["data"]["problem_short"] = result_data["problem_short"]


    # Finalize the "status" average and convert to a list
    aggregated_results = []
    for result_id, aggregated_data in aggregation.items():
        # Calculate the average "status"
        aggregated_data["data"]["status"] = round(
            aggregated_data["data"]["status"] / aggregated_data["data"]["test_index"], 7
        )
        # Add the aggregated result to the list
        aggregated_results.append({
            "id": result_id,
            "file": aggregated_data["file"],
            "data": aggregated_data["data"]
        })
        # Save the aggregated results to a JSON file
        output_file = os.path.join(BASE_DIR, "aggregated_results.json")
        with open(output_file, "w") as f:
            json.dump(aggregated_results, f, indent=4)

    return aggregated_results

def report_results(total_files_max):
    """
    Updates the README_FILE with a Markdown Header and a Markdown Table based on aggregated results.
    If the Markdown Header is not found, it appends the header and table to the end of the file.

    Parameters:
        total_files_max (int): The maximum number of result files to process.
    """
    # Generate aggregated results
    aggregated_results = aggregate_results(total_files_max)

    # Markdown Header, Legend, and Table
    markdown_header = "### Automated Tool Test Results\n"
    markdown_legend = "_\\* where Test 1 = the most recent deployed test_\n"
    markdown_table = []

    # Create the table header
    table_header = ["Tool ID", "Tool Version"] + [f"Test {i+1}" for i in range(max([len(data["data"]["status_item"]) for data in aggregated_results]))]
    markdown_table.append("| " + " | ".join(table_header) + " |")
    markdown_table.append("| " + " | ".join(["---"] * len(table_header)) + " |")

    # Create the table rows
    for result in aggregated_results:
        # Extract the short name of the Tool ID (content after the last "/")
        full_tool_id = result["data"]["tool_id"]
        short_tool_id = full_tool_id.split("/")[-1] if full_tool_id else "Unknown"

        tool_version = result["id"].rsplit("/", 1)[-1]
        # tool_version = result["data"]["tool_version"]
        row = [f"{short_tool_id}", tool_version]  # Use the short Tool ID as a placeholder link, Tool Version as plain text
        for i, status in enumerate(result["data"]["status_item"]):
            file_link = result["file"][i] if i < len(result["file"]) else "#"
            time_seconds = result["data"]["time_seconds_item"][i] if i < len(result["data"]["time_seconds_item"]) else "Unknown"
            
            # Round the time_seconds to the nearest integer
            if isinstance(time_seconds, (int, float)):
                time_seconds = round(time_seconds)
            
            # Handle problem_short
            if result["data"]["problem_short"]:
                problem_short = result["data"]["problem_short"]
            else:
                problem_short = ""
            
            if status == 1:
                tooltip = f"Success: completed in {time_seconds} seconds."
                emoji = f"[🟩]({file_link} \"{tooltip}\")"
            else:
                tooltip = f"Failure: completed in {time_seconds} seconds. {problem_short}"
                emoji = f"[🟥]({file_link} \"{tooltip}\")"
            row.append(emoji)
        markdown_table.append("| " + " | ".join(row) + " |")

    # Combine the Markdown Header, Legend, and Table
    markdown_content = markdown_header + "\n" + markdown_legend + "\n" + "\n".join(markdown_table) + "\n"

    # Check if the header exists in the README_FILE
    if not os.path.exists(README_FILE):
        print(f"README file '{README_FILE}' does not exist. Creating a new one.")
        with open(README_FILE, "w") as readme_file:
            readme_file.write(markdown_content)
        print(f"Markdown content written to {README_FILE}")
        return

    with open(README_FILE, "r") as readme_file:
        readme_content = readme_file.read()

    if markdown_header.strip() not in readme_content:
        # Append the header and table to the end of the file
        with open(README_FILE, "a") as readme_file:
            readme_file.write("\n" + markdown_content)
        print(f"Markdown content appended to {README_FILE}")
    else:
        # Replace the existing table after the header
        updated_content = []
        in_table_section = False
        for line in readme_content.splitlines():
            if line.strip() == markdown_header.strip():
                updated_content.append(line)
                updated_content.append("")  # Add a blank line after the header
                updated_content.append(markdown_legend)
                updated_content.extend(markdown_table)
                in_table_section = True
            elif in_table_section and line.startswith("|"):
                continue  # Skip existing table rows
            else:
                updated_content.append(line)
        with open(README_FILE, "w") as readme_file:
            readme_file.write("\n".join(updated_content))
        print(f"Markdown content updated in {README_FILE}")
    print(f"Updating README file at: {os.path.abspath(README_FILE)}")

def _clean_output(s):
    s = str(s)
    s = re.sub(r"[\"\'“”‘’`´|\[\]]", "", s)
    s = re.sub(r"[\n\t]", " ", s)
    s = s[:50] if len(s) > 50 else s
    s = s.strip()
    return s

# If this script is run directly, execute the aggregate_results function
if __name__ == "__main__":
    # Example: Process up to 5 result files
    report_results(total_files_max=135)
