import os
import re
import json
from collections import defaultdict

# Define the base directory
BASE_DIR = "../../reports/anvil-production/tool-tests"

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
                        # Extract the required parameters
                        result_data = {
                            "file": result_file,
                            "id": test_data.get("id"),
                            "data": {
                                "status": test_data.get("data", {}).get("status", "Unknown"),
                                "tool_id": test_data.get("data", {}).get("tool_id", "Unknown"),
                                "tool_version": test_data.get("data", {}).get("tool_version", "Unknown"),
                                "test_index": test_data.get("data", {}).get("test_index", "Unknown"),
                                "time_seconds": test_data.get("data", {}).get("time_seconds", 0),
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
            "status_calc": "average"
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

    return aggregated_results

# If this script is run directly, execute the aggregate_results function
if __name__ == "__main__":
    # Example: Process up to 5 result files
    aggregate_results(total_files_max=35)
