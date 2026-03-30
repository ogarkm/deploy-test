import json
import requests
import time
import re

def normalize_string(s):
    """Removes spaces, punctuation, and lowercases the string for robust comparison."""
    if not s:
        return ""
    return re.sub(r'[^a-z0-9]', '', str(s).lower())

def is_name_match(json_name, api_data):
    """Checks if the JSON name matches any of the anime's titles from MAL."""
    norm_json = normalize_string(json_name)
    
    # Gather all possible titles from the API response
    titles =[]
    if api_data.get("title"): titles.append(api_data["title"])
    if api_data.get("title_english"): titles.append(api_data["title_english"])
    if api_data.get("title_japanese"): titles.append(api_data["title_japanese"])
    
    # Jikan v4 also provides a 'titles' array
    for t_obj in api_data.get("titles",[]):
        if "title" in t_obj:
            titles.append(t_obj["title"])
            
    for syn in api_data.get("title_synonyms",[]):
        titles.append(syn)
        
    # Check for a match
    for t in titles:
        norm_t = normalize_string(t)
        # Using "in" allows us to match variations like "Spy x Family Part 2" with "Spy x Family Cour 2" 
        # as long as the base string closely aligns, but it leans toward exact matches.
        if norm_json == norm_t or norm_json in norm_t or norm_t in norm_json:
            return True
            
    return False

def get_anime_by_id(mal_id, retries=5):
    """Fetches anime details by MAL ID with retry and rate-limit handling."""
    url = f"https://api.jikan.moe/v4/anime/{mal_id}"
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                return response.json().get("data")
            elif response.status_code == 404:
                return None  # ID does not exist
            elif response.status_code == 429:
                print("  [!] Rate limited. Waiting 1 second...")
                time.sleep(1)
                attempt += 1
            else:
                print(f"  [!] HTTP Error {response.status_code}. Retrying...")
                time.sleep(1)
                attempt += 1
        except requests.exceptions.RequestException:
            print("  [!] Network error. Retrying...")
            time.sleep(1)
            attempt += 1
    return None

def search_anime(query, retries=5):
    """Searches Jikan for the anime by name. Sorts by 'members' (popularity)."""
    url = "https://api.jikan.moe/v4/anime"
    params = {
        "q": query,
        "limit": 7
    }
    
    attempt = 0
    while attempt < retries:
        try:
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                return response.json().get("data",[])
            elif response.status_code == 429:
                print("  [!] Rate limited. Waiting 1 second...")
                time.sleep(1)
                attempt += 1
            else:
                print(f"  [!] HTTP Error {response.status_code}. Retrying...")
                time.sleep(1)
                attempt += 1
        except requests.exceptions.RequestException:
            print("  [!] Network error. Retrying...")
            time.sleep(1)
            attempt += 1

    return[]

def prompt_user_choice(anime_name, current_id, results):
    """Displays the search results and asks the user to pick the correct one."""
    print(f"\n==================================================")
    print(f"🔍 Searching for manually: {anime_name} (Current ID: {current_id})")
    print(f"==================================================")
    
    if not results:
        print("  [!] No results found on MyAnimeList.")
        return None

    for i, res in enumerate(results):
        title = res.get("title_english") or res.get("title")
        media_type = res.get("type", "Unknown")
        year = res.get("year", "N/A")
        mal_id = res["mal_id"]
        
        print(f"  [{i + 1}] {title} ({media_type}, {year}) - ID: {mal_id}")
    
    print("  [0] Skip / Keep current")
    print("  [9] Enter a custom MAL ID manually")
    
    while True:
        try:
            choice = input("\nSelect the correct anime (0-5, or 9): ").strip()
            if choice == "":
                continue
                
            choice = int(choice)
            
            if choice == 0:
                print("  -> Skipping. Kept original data.")
                return None
            elif choice == 9:
                custom_id = int(input("  -> Enter custom MAL ID: ").strip())
                return get_anime_by_id(custom_id)
            elif 1 <= choice <= len(results):
                return results[choice - 1]
            else:
                print("  [!] Invalid choice. Please enter a number from the list.")
        except ValueError:
            print("  [!] Please enter a valid number.")

def main():
    file_path = "content.json"
    
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        print(f"Error: Could not find '{file_path}'.")
        return

    for section in data.get("sections", []):
        for item in section.get("items",[]):
            name = item.get("name")
            current_id = item.get("id")
            current_image = item.get("image")
            
            if not name or not current_id:
                continue
                
            print(f"\nChecking: {name} (ID: {current_id})")
            
            # 1. Fetch data for the current ID
            api_data = get_anime_by_id(current_id)
            time.sleep(0.4) # Respect Jikan's 3 requests/second rate limit
            
            # 2. Check if ID matches Name
            if api_data and is_name_match(name, api_data):
                # Name matches! Now verify the image
                images = api_data.get("images", {}).get("jpg", {})
                best_image = images.get("large_image_url") or images.get("image_url")
                
                if current_image == best_image:
                    print("  ✅ ID and Image are both correct. Skipping.")
                else:
                    print("  ⚠️ ID is correct, but Image is outdated. Updating image automatically...")
                    if best_image:
                        item["image"] = best_image
                        print(f"  ✅ Updated Image: {best_image}")
            else:
                # Name does not match or ID is invalid. Prompt user.
                print("  ❌ ID does NOT match the Name (or ID is invalid). Searching MAL...")
                
                results = search_anime(name)
                time.sleep(0.4)
                
                selected_anime = prompt_user_choice(name, current_id, results)
                
                if selected_anime:
                    new_id = selected_anime["mal_id"]
                    images = selected_anime.get("images", {}).get("jpg", {})
                    new_image_url = images.get("large_image_url") or images.get("image_url")
                    
                    item["id"] = new_id
                    if new_image_url:
                        item["image"] = new_image_url
                        
                    print(f"  ✅ Updated '{name}' -> ID: {new_id} | Image: {new_image_url}")

    # Save the updated JSON back to the file
    with open(file_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)
    
    print("\n🎉 Done! All selected IDs and Images have been verified/updated and saved to 'content.json'.")

if __name__ == "__main__":
    main()