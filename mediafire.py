#!/usr/bin/env python3

import base64
import hashlib
import http.client
import urllib.parse
from re import findall
from time import sleep, time
from io import BytesIO
from gzip import GzipFile
from requests import get, Session
from gazpacho import Soup
from argparse import ArgumentParser
from os import path, makedirs, remove, chdir, getcwd
from threading import BoundedSemaphore, Thread, Event, Lock
import sys


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


NON_ALPHANUM_FILE_OR_FOLDER_NAME_CHARACTERS = "-_. "
NON_ALPHANUM_FILE_OR_FOLDER_NAME_CHARACTER_REPLACEMENT = "-"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Accept-Encoding": "gzip",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}


class ProgressTracker:
    def __init__(self, total_files, total_size):
        self.total_files = total_files
        self.total_size = total_size
        self.completed_files = 0
        self.downloaded_bytes = 0
        self.failed_files = 0
        self.skipped_files = 0
        self.lock = Lock()
        self.start_time = time()
        self.file_speeds = []
        self.last_update_time = time()
        self.last_bytes = 0
        
    def update(self, bytes_downloaded, file_completed=False, file_failed=False, file_skipped=False):
        with self.lock:
            self.downloaded_bytes += bytes_downloaded
            if file_completed:
                self.completed_files += 1
            if file_failed:
                self.failed_files += 1
            if file_skipped:
                self.skipped_files += 1
            
            current_time = time()
            if current_time - self.last_update_time >= 0.5:
                self.print_progress()
                self.last_update_time = current_time
    
    def print_progress(self):
        elapsed_time = time() - self.start_time
        if elapsed_time == 0:
            return
            
        file_progress = (self.completed_files / self.total_files) * 100 if self.total_files > 0 else 0
        byte_progress = (self.downloaded_bytes / self.total_size) * 100 if self.total_size > 0 else 0
        
        speed = self.downloaded_bytes / elapsed_time
        remaining_bytes = self.total_size - self.downloaded_bytes
        eta_seconds = remaining_bytes / speed if speed > 0 else 0
        
        downloaded_mb = self.downloaded_bytes / (1024 * 1024)
        total_mb = self.total_size / (1024 * 1024)
        speed_mb = speed / (1024 * 1024)
        
        eta_hours = int(eta_seconds // 3600)
        eta_min = int((eta_seconds % 3600) // 60)
        eta_sec = int(eta_seconds % 60)
        
        eta_str = ""
        if eta_hours > 0:
            eta_str = f"{eta_hours}h {eta_min}m"
        elif eta_min > 0:
            eta_str = f"{eta_min}m {eta_sec}s"
        else:
            eta_str = f"{eta_sec}s"
        
        status = f"{bcolors.OKCYAN}[{self.completed_files}/{self.total_files} files"
        if self.failed_files > 0:
            status += f" | {bcolors.FAIL}{self.failed_files} failed{bcolors.OKCYAN}"
        if self.skipped_files > 0:
            status += f" | {self.skipped_files} skipped{bcolors.OKCYAN}"
        status += f"] {downloaded_mb:.1f}/{total_mb:.1f} MB ({byte_progress:.1f}%) | "
        status += f"{speed_mb:.2f} MB/s | ETA: {eta_str}{bcolors.ENDC}"
        
        print(f"\r{status}", end='', flush=True)
    
    def finish(self):
        elapsed_time = time() - self.start_time
        total_mb = self.total_size / (1024 * 1024)
        avg_speed = self.downloaded_bytes / elapsed_time / (1024 * 1024) if elapsed_time > 0 else 0
        
        minutes = int(elapsed_time // 60)
        seconds = int(elapsed_time % 60)
        
        print(f"\n{bcolors.OKGREEN}{bcolors.BOLD}Download completed!{bcolors.ENDC}")
        print(f"{bcolors.OKGREEN}Total: {self.completed_files} files | {total_mb:.1f} MB | "
              f"Time: {minutes}m {seconds}s | Avg speed: {avg_speed:.2f} MB/s{bcolors.ENDC}")
        if self.failed_files > 0:
            print(f"{bcolors.FAIL}Failed: {self.failed_files} files{bcolors.ENDC}")
        if self.skipped_files > 0:
            print(f"{bcolors.WARNING}Skipped: {self.skipped_files} files (already downloaded){bcolors.ENDC}")


def hash_file(filename: str) -> str:
    h = hashlib.sha256()
    with open(filename, "rb") as file:
        chunk = 0
        while chunk != b"":
            chunk = file.read(1024)
            h.update(chunk)
    return h.hexdigest()


def normalize_file_or_folder_name(filename: str) -> str:
    return "".join(
        [
            (
                char
                if (
                    char.isalnum()
                    or char in NON_ALPHANUM_FILE_OR_FOLDER_NAME_CHARACTERS
                )
                else NON_ALPHANUM_FILE_OR_FOLDER_NAME_CHARACTER_REPLACEMENT
            )
            for char in filename
        ]
    )


def print_error(link: str, filename: str = ""):
    error_msg = f"\n{bcolors.FAIL}Error downloading"
    if filename:
        error_msg += f" {filename}"
    error_msg += f": File deleted or dangerous file blocked{bcolors.ENDC}"
    print(error_msg)


def format_size(size_bytes):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def interactive_mode():
    print(f"\n{bcolors.HEADER}{bcolors.BOLD}=== MediaFire Bulk Downloader ==={bcolors.ENDC}\n")
    
    while True:
        url = input(f"{bcolors.OKCYAN}Enter MediaFire folder/file URL: {bcolors.ENDC}").strip()
        if url:
            break
        print(f"{bcolors.FAIL}URL cannot be empty. Please try again.{bcolors.ENDC}")
    
    output = input(f"{bcolors.OKCYAN}Enter destination folder (press Enter for current directory): {bcolors.ENDC}").strip()
    if not output:
        output = "."
    
    while True:
        threads = input(f"{bcolors.OKCYAN}Number of threads (press Enter for 20): {bcolors.ENDC}").strip()
        if not threads:
            threads = 20
            break
        try:
            threads = int(threads)
            if threads < 1 or threads > 50:
                print(f"{bcolors.WARNING}Please enter a number between 1 and 50{bcolors.ENDC}")
                continue
            break
        except ValueError:
            print(f"{bcolors.FAIL}Invalid number. Please try again.{bcolors.ENDC}")
    
    print(f"\n{bcolors.OKBLUE}Starting download...{bcolors.ENDC}\n")
    return url, output, threads


def main():
    if len(sys.argv) == 1:
        mediafire_url, output_path, threads_num = interactive_mode()
    else:
        parser = ArgumentParser(
            "mediafire_bulk_downloader", 
            usage="python mediafire.py <mediafire_url> [-o <output_path>] [-t <num_threads>]"
        )
        parser.add_argument(
            "mediafire_url", 
            help="The URL of the file or folder to be downloaded"
        )
        parser.add_argument(
            "-o",
            "--output",
            help="The path of the desired output folder",
            required=False,
            default=".",
        )
        parser.add_argument(
            "-t",
            "--threads",
            help="Number of threads to use (default: 20)",
            type=int,
            default=20,
            required=False,
        )
        args = parser.parse_args()
        mediafire_url = args.mediafire_url
        output_path = args.output
        threads_num = args.threads

    folder_or_file = findall(
        r"mediafire\.com/(folder|file|file_premium)\/([a-zA-Z0-9]+)", mediafire_url
    )

    if not folder_or_file:
        print(f"{bcolors.FAIL}Invalid MediaFire link{bcolors.ENDC}")
        exit(1)

    t, key = folder_or_file[0]

    try:
        if t in {"file", "file_premium"}:
            get_file(key, output_path)
        elif t == "folder":
            get_folders(key, output_path, threads_num, first=True)
        else:
            print(f"{bcolors.FAIL}Invalid link type{bcolors.ENDC}")
            exit(1)
    except KeyboardInterrupt:
        print(f"\n{bcolors.WARNING}Download cancelled by user{bcolors.ENDC}")
        exit(0)
    except Exception as e:
        print(f"\n{bcolors.FAIL}Error: {str(e)}{bcolors.ENDC}")
        exit(1)


def get_files_or_folders_api_endpoint(
    filefolder: str, folder_key: str, chunk: int = 1, info: bool = False
) -> str:
    return (
        f"https://www.mediafire.com/api/1.4/folder"
        f"/{'get_info' if info else 'get_content'}.php?r=utga&content_type={filefolder}"
        f"&filter=all&order_by=name&order_direction=asc&chunk={chunk}"
        f"&version=1.5&folder_key={folder_key}&response_format=json"
    )


def get_info_endpoint(file_key: str) -> str:
    return f"https://www.mediafire.com/api/file/get_info.php?quick_key={file_key}&response_format=json"


def get_folders(
    folder_key: str, folder_name: str, threads_num: int, first: bool = False
) -> None:
    if first:
        print(f"{bcolors.OKBLUE}Fetching folder information...{bcolors.ENDC}")
        r = get(get_files_or_folders_api_endpoint("folder", folder_key, info=True))
        if r.status_code != 200:
            message = r.json()["response"]["message"]
            print(f"{bcolors.FAIL}{message}{bcolors.ENDC}")
            exit(1)

        folder_info = r.json()["response"]["folder_info"]
        folder_name_normalized = normalize_file_or_folder_name(folder_info["name"])
        folder_name = path.join(folder_name, folder_name_normalized)
        
        print(f"{bcolors.OKGREEN}Folder: {folder_info['name']}{bcolors.ENDC}")
        print(f"{bcolors.OKGREEN}Files: {folder_info.get('file_count', 'Unknown')}{bcolors.ENDC}")
        print(f"{bcolors.OKGREEN}Destination: {path.abspath(folder_name)}{bcolors.ENDC}\n")

    if not path.exists(folder_name):
        makedirs(folder_name)
    chdir(folder_name)

    download_folder(folder_key, threads_num)

    folder_content = get(
        get_files_or_folders_api_endpoint("folders", folder_key)
    ).json()["response"]["folder_content"]

    if "folders" in folder_content:
        for folder in folder_content["folders"]:
            print(f"\n{bcolors.HEADER}Entering subfolder: {folder['name']}{bcolors.ENDC}")
            get_folders(folder["folderkey"], folder["name"], threads_num)
            chdir("..")


def download_folder(folder_key: str, threads_num: int) -> None:
    data = []
    chunk = 1
    more_chunks = True

    try:
        while more_chunks:
            r_json = get(
                get_files_or_folders_api_endpoint("files", folder_key, chunk=chunk)
            ).json()
            more_chunks = r_json["response"]["folder_content"]["more_chunks"] == "yes"
            data += r_json["response"]["folder_content"]["files"]
            chunk += 1
    except KeyError:
        print(f"{bcolors.FAIL}Invalid folder or API error{bcolors.ENDC}")
        return

    if not data:
        print(f"{bcolors.WARNING}No files found in this folder{bcolors.ENDC}")
        return

    total_size = sum(int(file.get("size", 0)) for file in data)
    
    print(f"{bcolors.OKBLUE}Found {len(data)} files ({format_size(total_size)}){bcolors.ENDC}")
    
    progress = ProgressTracker(len(data), total_size)
    
    event = Event()
    threadLimiter = BoundedSemaphore(threads_num)
    total_threads: list[Thread] = []

    for file in data:
        total_threads.append(
            Thread(
                target=download_file,
                args=(file, event, threadLimiter, progress),
            )
        )

    for thread in total_threads:
        thread.start()

    try:
        while True:
            if all(not t.is_alive() for t in total_threads):
                break
            sleep(0.1)
    except KeyboardInterrupt:
        print(f"\n{bcolors.WARNING}Closing all threads...{bcolors.ENDC}")
        event.set()
        for thread in total_threads:
            thread.join()
        print(f"{bcolors.WARNING}{bcolors.BOLD}Download interrupted{bcolors.ENDC}")
        exit(0)
    
    progress.finish()


def get_file(key: str, output_path: str = None) -> None:
    print(f"{bcolors.OKBLUE}Fetching file information...{bcolors.ENDC}")
    file_data = get(get_info_endpoint(key)).json()["response"]["file_info"]

    if output_path:
        current_dir = getcwd()
        if not path.exists(output_path):
            makedirs(output_path)
        filename = path.join(output_path, file_data["filename"])
        chdir(output_path)
    else:
        filename = file_data["filename"]

    total_size = int(file_data.get("size", 0))
    print(f"{bcolors.OKGREEN}File: {file_data['filename']} ({format_size(total_size)}){bcolors.ENDC}\n")
    
    progress = ProgressTracker(1, total_size)

    download_file(file_data, progress=progress)

    if output_path:
        chdir(current_dir)
    
    progress.finish()
    return filename


def download_file(
    file: dict, 
    event: Event = None, 
    limiter: BoundedSemaphore = None, 
    progress: ProgressTracker = None
) -> None:
    if limiter:
        limiter.acquire()

    download_link = file["links"]["normal_download"]
    filename = normalize_file_or_folder_name(file["filename"])
    file_size = int(file.get("size", 0))

    if path.exists(filename):
        if hash_file(filename) == file["hash"]:
            if progress:
                progress.update(file_size, file_skipped=True)
            if limiter:
                limiter.release()
            return
        else:
            remove(filename)

    if event and event.is_set():
        if limiter:
            limiter.release()
        return

    try:
        parsed_url = urllib.parse.urlparse(download_link)
        conn = http.client.HTTPSConnection(parsed_url.netloc, timeout=30)
        conn.request(
            "GET",
            parsed_url.path + ("?" + parsed_url.query if parsed_url.query else ""),
            headers=HEADERS,
        )

        response = conn.getresponse()

        if response.getheader("Content-Encoding") == "gzip":
            compressed_data = response.read()
            conn.close()
            
            with GzipFile(fileobj=BytesIO(compressed_data)) as f:
                html = f.read().decode("utf-8")
                soup = Soup(html)
                
                download_button = soup.find("a", {"id": "downloadButton"})
                
                if not download_button:
                    if progress:
                        progress.update(0, file_failed=True)
                    else:
                        print_error(download_link, filename)
                    if limiter:
                        limiter.release()
                    return
                
                download_url = download_button.attrs.get("href")
                
                if not download_url:
                    if progress:
                        progress.update(0, file_failed=True)
                    else:
                        print_error(download_link, filename)
                    if limiter:
                        limiter.release()
                    return
                
                parsed_url = urllib.parse.urlparse(download_url)
                conn = http.client.HTTPSConnection(parsed_url.netloc, timeout=30)
                conn.request(
                    "GET",
                    parsed_url.path + ("?" + parsed_url.query if parsed_url.query else ""),
                    headers=HEADERS,
                )
                response = conn.getresponse()

        if 400 <= response.status < 600:
            conn.close()
            if progress:
                progress.update(0, file_failed=True)
            else:
                print_error(download_link, filename)
            if limiter:
                limiter.release()
            return

        buffer_size = 131072
        
        with open(filename, "wb") as f:
            while True:
                chunk = response.read(buffer_size)

                if event and event.is_set():
                    conn.close()
                    f.close()
                    remove(filename)
                    if limiter:
                        limiter.release()
                    return
                    
                if not chunk:
                    break
                    
                f.write(chunk)
                
                if progress:
                    progress.update(len(chunk))

        conn.close()

        if progress:
            progress.update(0, file_completed=True)
        else:
            print(f"{bcolors.OKGREEN}{filename} downloaded{bcolors.ENDC}")

    except Exception as e:
        if path.exists(filename):
            remove(filename)
        if progress:
            progress.update(0, file_failed=True)
        else:
            print(f"\n{bcolors.FAIL}Error downloading {filename}: {str(e)}{bcolors.ENDC}")
    finally:
        if limiter:
            limiter.release()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{bcolors.WARNING}Exiting...{bcolors.ENDC}")
        exit(0)
