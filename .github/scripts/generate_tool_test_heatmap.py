import os
import re

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
    
    # Define the regex pattern for the datetime format 'YYYY-MM-DD-HH-MM-SS'
    datetime_pattern = re.compile(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}")

    # Initialize an empty list to store matching folder paths
    matching_folders = []
    
    # Check if the base directory exists
    if not os.path.exists(BASE_DIR):
        print(f"Directory '{BASE_DIR}' does not exist.")
        return matching_folders

    # Get and sort the directory entries in descending order
    sorted_entries = sorted(os.listdir(BASE_DIR), reverse=True)

    # Iterate through the sorted entries in the base directory
    for entry in sorted_entries:
        entry_path = os.path.join(BASE_DIR, entry)
        
        # Check if the entry is a directory and contains the datetime pattern
        if os.path.isdir(entry_path) and datetime_pattern.search(entry):
            matching_folders.append(f"{entry_path}/results.json")

        # Stop adding folders if the limit is reached
        if len(matching_folders) >= total_files_max:
            break
    
    # Output the list of matching folder paths to the console log
    print("Matching folders:")
    for folder in matching_folders:
        print(folder)
    
    return matching_folders

# If this script is run directly, execute the function with a sample limit
if __name__ == "__main__":
    # Example: Limit the results to 5 folders
    get_results_files(total_files_max=5)
