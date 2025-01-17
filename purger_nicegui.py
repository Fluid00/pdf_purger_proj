#!/usr/bin/env python3
import os
import json
import logging
import asyncio
import concurrent.futures
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from nicegui import app, ui, run, events
from nicegui.client import Client
import fitz  # PyMuPDF
import platform
import uuid

# Windows-specific imports
if platform.system() == "Windows":
    import win32api

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('pdf_purger.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("pdf_purger")

class ProcessManager:
    """Handle process state management and concurrency control."""
    def __init__(self):
        self._processing = False
        self._stop_requested = False
        self._active_folders = set()
        
    def start_processing(self, folder_path: str = None) -> bool:
        """Start processing with optional folder tracking."""
        if self.is_processing():
            return False
            
        self._processing = True
        self._stop_requested = False
        
        if folder_path:
            self._active_folders.add(folder_path)

        # Update storage if in page context
        try:
            app.storage.user['processing'] = True
            app.storage.user['stop_requested'] = False
            app.storage.user['active_folders'] = list(self._active_folders)
        except RuntimeError:
            pass
            
        return True
        
    def stop_processing(self) -> bool:
        """Request processing stop with notification."""
        if not self.is_processing():
            return False
            
        self._stop_requested = True
        
        # Update storage if in page context
        try:
            app.storage.user['stop_requested'] = True
        except RuntimeError:
            pass
            
        ui.notify("Processing will stop after current file completes", 
                 color="warning", 
                 position="top")
        return True
        
    def reset(self):
        """Reset all process state."""
        self._processing = False
        self._stop_requested = False
        self._active_folders.clear()
        
        # Update storage if in page context
        try:
            app.storage.user['processing'] = False
            app.storage.user['stop_requested'] = False
            app.storage.user['active_folders'] = []
        except RuntimeError:
            pass
        
    def is_processing(self) -> bool:
        """Check if processing is active."""
        return self._processing and not self._stop_requested
        
    def is_folder_active(self, folder_path: str) -> bool:
        """Check if specific folder is being processed."""
        return folder_path in self._active_folders

    def sync_with_storage(self):
        """Synchronize state with page storage if available."""
        try:
            if 'processing' in app.storage.user:
                self._processing = app.storage.user['processing']
            if 'stop_requested' in app.storage.user:
                self._stop_requested = app.storage.user['stop_requested']
            if 'active_folders' in app.storage.user:
                self._active_folders = set(app.storage.user['active_folders'])
        except RuntimeError:
            # Not in page context, use local state
            pass

class ProgressManager:
    """Handle progress tracking and message management."""
    def __init__(self):
        self.messages: List[str] = []
        self.current_progress: float = 0.0
        self.current_text: str = ""
        self.total_files: int = 0
        self.processed_files: int = 0
        
    def start_batch(self, total_files: int):
        """Initialize for a new batch of files."""
        self.total_files = total_files
        self.processed_files = 0
        self.current_progress = 0.0
        self.messages.clear()
        
    def add_message(self, message: str, message_type: str = "info"):
        """Add a message with optional type (info/warning/error)."""
        prefix = {
            "info": "ℹ️",
            "warning": "⚠️",
            "error": "❌",
            "success": "✅"
        }.get(message_type, "")
        
        formatted_message = f"{prefix} {message}" if prefix else message
        self.messages.append(formatted_message)
        logger.info(message)
        
    def update_progress(self, progress: float, text: str):
        """Update progress state."""
        self.current_progress = min(max(progress, 0.0), 1.0)
        self.current_text = text
        
    def increment_processed(self):
        """Increment processed files counter and update progress."""
        self.processed_files += 1
        if self.total_files > 0:
            self.current_progress = self.processed_files / self.total_files
            
    def get_state(self) -> Dict:
        """Get current progress state."""
        return {
            'messages': self.messages.copy(),
            'progress': self.current_progress,
            'text': self.current_text,
            'processed': self.processed_files,
            'total': self.total_files
        }
        
    def clear(self):
        """Reset progress state."""
        self.messages.clear()
        self.current_progress = 0.0
        self.current_text = ""
        self.total_files = 0
        self.processed_files = 0

# --- State Management Functions ---
def load_state() -> List[str]:
    """Load folder paths from state file."""
    try:
        state_file = Path('purger_state.json')
        if not state_file.exists():
            return []
        with open(state_file, 'r', encoding='utf-8') as f:
            state = json.load(f)
            return state.get('folders', [])
    except Exception as e:
        logger.error(f"Error loading state: {e}")
        return []

def save_state(folder_paths: List[str]):
    """Save folder paths to state file."""
    try:
        state_file = Path('purger_state.json')
        state = {'folders': [str(path) for path in folder_paths if path]}
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

# --- File Tracking Functions ---
def load_processed_files(folder_path: str) -> Set[str]:
    """Load set of processed files."""
    try:
        processed_file = Path(folder_path) / "processed_files.json"
        if processed_file.exists():
            with open(processed_file, 'r') as f:
                return set(json.load(f))
        return set()
    except Exception as e:
        logger.error(f"Error loading processed files: {e}")
        return set()

def save_processed_file(folder_path: str, file_path: str):
    """Save processed file to tracking."""
    try:
        processed_file = Path(folder_path) / "processed_files.json"
        processed = load_processed_files(folder_path)
        processed.add(str(file_path))
        
        with open(processed_file, 'w') as f:
            json.dump(list(processed), f)
    except Exception as e:
        logger.error(f"Error saving processed file: {e}")

class FileTracker:
    """Track processed and skipped files."""
    def __init__(self, folder_path: str):
        self.folder_path = Path(folder_path)
        self.purged_files = self._load_file_set("purged_files.txt")
        self.skipped_files = self._load_file_set("skipped_files.txt")
        
    def _load_file_set(self, filename: str) -> Set[str]:
        """Load a set of files from tracking file."""
        file_path = self.folder_path / filename
        if file_path.exists():
            with open(file_path, 'r') as f:
                return {line.strip() for line in f if line.strip()}
        return set()
        
    def _save_to_file(self, filename: str, file_path: str):
        """Save a file path to tracking file."""
        with open(self.folder_path / filename, 'a') as f:
            f.write(f"{file_path}\n")
            
    def is_processed(self, file_path: str) -> bool:
        """Check if file has been processed or skipped."""
        return (file_path in self.purged_files or 
                file_path in self.skipped_files)
        
    def mark_purged(self, file_path: str):
        """Mark file as successfully purged."""
        if file_path not in self.purged_files:
            self.purged_files.add(file_path)
            self._save_to_file("purged_files.txt", file_path)
            
    def mark_skipped(self, file_path: str):
        """Mark file as skipped."""
        if file_path not in self.skipped_files:
            self.skipped_files.add(file_path)
            self._save_to_file("skipped_files.txt", file_path)
            
class PDFProcessor:
    """Handle PDF processing operations."""
    
    @staticmethod
    def repair_xrefs(doc: fitz.Document) -> bool:
        """Repair cross-references in PDF document."""
        try:
            if hasattr(doc, 'xref_repair'):
                doc.xref_repair()
            if hasattr(doc, 'xref_compress'):
                doc.xref_compress()
            return True
        except Exception as e:
            logger.warning(f"Warning during xref repair: {e}")
            return False

    @staticmethod
    def replace_white_with_black(page: fitz.Page):
        """Replace white text with black text."""
        try:
            text_blocks = []
            try:
                text_dict = page.get_text("dict")
                blocks = text_dict.get('blocks', [])
                for block in blocks:
                    if block.get("type", -1) == 0:  # Text block
                        text_blocks.append(block)
            except Exception as e:
                logger.warning(f"Error getting text blocks on page {page.number + 1}: {e}")

            for block in text_blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span["color"] in (0xFFFFFF, 0xFFFFFF00):  # White text
                            page.insert_text(
                                fitz.Rect(span["bbox"]).tl,
                                span["text"],
                                fontname=span["font"],
                                fontsize=span["size"],
                                color=0,  # black
                                overlay=True
                            )
        except Exception as e:
            logger.warning(f"Error modifying text colors on page {page.number + 1}: {e}")

    @staticmethod
    async def process_pdf(filepath: str, progress_callback) -> Tuple[bool, str]:
        """
        Process a single PDF file.
        Returns: (success: bool, message: str)
        """
        file_name = os.path.basename(filepath)
        temp_file = filepath + str(uuid.uuid4()) + ".temp"
        doc = None
        successful = False

        try:
            # Initial open and repair if needed
            try:
                doc = fitz.open(filepath)
            except Exception:
                doc = fitz.open(filepath)
                doc.repair()
                doc.save(temp_file,
                        garbage=4,
                        clean=True,
                        deflate=True,
                        pretty=False,
                        linear=True)
                doc.close()
                
                # Verify repair
                doc = fitz.open(temp_file)
                os.replace(temp_file, filepath)
                doc = fitz.open(filepath)
                successful = True

            if not PDFProcessor.repair_xrefs(doc):
                progress_callback({
                    'type': 'warning',
                    'message': f"Initial xref repair failed for {file_name}"
                })

            # Process pages using ThreadPoolExecutor
            with concurrent.futures.ThreadPoolExecutor() as executor:
                # Find empty pages
                empty_pages = []
                page_texts = list(executor.map(
                    lambda p: (p, doc[p].get_text().strip()),
                    range(len(doc))
                ))
                empty_pages = [p for p, text in page_texts if not text]

                # Delete empty pages in reverse order
                for page_num in reversed(empty_pages):
                    try:
                        doc.delete_page(page_num)
                    except Exception as e:
                        logger.warning(f"Error deleting page {page_num}: {e}")

                # Process remaining pages
                total_pages = len(doc)
                for p_num in range(total_pages):
                    if app.storage.user.stop_requested:
                        raise asyncio.CancelledError("Processing stopped by user")

                    page = doc[p_num]
                    
                    # Remove images
                    try:
                        image_list = page.get_images(full=True)
                        for img in image_list:
                            try:
                                page.delete_image(img[0])
                            except Exception as e:
                                logger.warning(f"Error deleting image: {e}")
                    except Exception as e:
                        logger.warning(f"Error getting images: {e}")

                    # Fix text colors in parallel
                    executor.submit(PDFProcessor.replace_white_with_black, page)
                    
                    # Update progress
                    progress = (p_num + 1) / total_pages
                    progress_callback({
                        'type': 'progress',
                        'progress': progress,
                        'message': f"Processing page {p_num + 1}/{total_pages} in {file_name}"
                    })

            # Final cleanup and save
            PDFProcessor.repair_xrefs(doc)
            doc.save(filepath,
                    garbage=4,
                    deflate=True,
                    clean=True,
                    linear=True)
            successful = True

        except asyncio.CancelledError:
            logger.info(f"Processing cancelled for {file_name}")
            raise

        except Exception as e:
            logger.error(f"Error processing {file_name}: {e}")
            return False, f"Error processing {file_name}: {str(e)}"

        finally:
            if doc:
                doc.close()
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

        return successful, f"Successfully processed {file_name}" if successful else f"Failed to process {file_name}"

class FileScanner:
    """Handle file scanning and validation."""
    
    @staticmethod
    def scan_pdfs(folder_path: str, file_tracker: FileTracker) -> Tuple[List[str], List[str]]:
        """
        Scan folder for PDFs to process.
        Returns: (file_list, messages)
        """
        file_list = []
        messages = []
        
        try:
            for root, _, files in os.walk(folder_path):
                for file in files:
                    if file.lower().startswith("bilag_") and file.lower().endswith(".pdf"):
                        file_path = os.path.join(root, file)
                        if not file_tracker.is_processed(file_path):
                            file_list.append(file_path)

            messages.extend([
                f"Found {len(file_list)} new PDF files to process",
                f"({len(file_tracker.purged_files)} previously processed, "
                f"{len(file_tracker.skipped_files)} skipped)"
            ])

        except Exception as e:
            logger.error(f"Error scanning folder {folder_path}: {e}")
            messages.append(f"Error scanning folder: {str(e)}")
            
        return file_list, messages

    @staticmethod
    def prepare_folders(folder_path: str) -> Tuple[bool, str]:
        """
        Prepare folder structure for processing.
        Returns: (success, message)
        """
        try:
            folder = Path(folder_path)
            
            # Create main folder if it doesn't exist
            folder.mkdir(parents=True, exist_ok=True)
            
            # Create required subfolders
            (folder / "to_delete").mkdir(exist_ok=True)
            (folder / "logs").mkdir(exist_ok=True)
            
            # Initialize tracking files if they don't exist
            for track_file in ["purged_files.txt", "skipped_files.txt"]:
                track_path = folder / track_file
                if not track_path.exists():
                    track_path.touch()
                    
            return True, "Folder structure prepared successfully"
            
        except Exception as e:
            logger.error(f"Error preparing folders: {e}")
            return False, f"Error preparing folders: {str(e)}"

    @staticmethod
    def cleanup_temp_files(folder_path: str):
        """Remove temporary files from previous runs."""
        try:
            folder = Path(folder_path)
            for item in folder.rglob("*.temp"):
                try:
                    item.unlink()
                except Exception as e:
                    logger.warning(f"Could not delete temp file {item}: {e}")
        except Exception as e:
            logger.error(f"Error during temp file cleanup: {e}")

    @staticmethod
    def validate_folder(folder_path: str) -> Tuple[bool, str]:
        """
        Validate folder path and accessibility.
        Returns: (is_valid, message)
        """
        try:
            folder = Path(folder_path)
            if not folder.exists():
                return False, "Folder does not exist"
            if not folder.is_dir():
                return False, "Path is not a directory"
            # Test write permissions by trying to create a test file
            test_file = folder / ".test_write_permission"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception:
                return False, "No write permission in folder"
            return True, "Folder is valid and accessible"
        except Exception as e:
            return False, f"Error validating folder: {str(e)}"

    @staticmethod
    def get_folder_stats(folder_path: str, file_tracker: FileTracker) -> Dict[str, int]:
        """Get folder statistics."""
        try:
            stats = {
                'total_files': 0,
                'processed_files': len(file_tracker.purged_files),
                'skipped_files': len(file_tracker.skipped_files),
                'pending_files': 0
            }
            
            for root, _, files in os.walk(folder_path):
                for file in files:
                    if file.lower().startswith("bilag_") and file.lower().endswith(".pdf"):
                        stats['total_files'] += 1
                        
            stats['pending_files'] = (
                stats['total_files'] 
                - stats['processed_files'] 
                - stats['skipped_files']
            )
            
            return stats
        except Exception as e:
            logger.error(f"Error getting folder stats: {e}")
            return {
                'total_files': 0,
                'processed_files': 0,
                'skipped_files': 0,
                'pending_files': 0
            }
# --- File Processing Functions ---
async def process_pdf_folder(folder_path: str, thread_count: int, progress_callback, process_manager: ProcessManager):
    """
    Process a folder of PDF files.
    """
    progress_manager = ProgressManager()
    file_tracker = FileTracker(folder_path)

    success, prep_message = FileScanner.prepare_folders(folder_path)
    if not success:
        progress_manager.add_message(prep_message, "error")
        return False, prep_message

    FileScanner.cleanup_temp_files(folder_path)

    file_list, scan_messages = FileScanner.scan_pdfs(folder_path, file_tracker)
    for msg in scan_messages:
        progress_manager.add_message(msg)

    progress_manager.start_batch(len(file_list))
    progress_callback(progress_manager.get_state())

    if not file_list:
        progress_manager.add_message("No new PDF files found to process.", "warning")
        progress_callback(progress_manager.get_state())
        return True, "No new files found."

    process_manager.start_processing(folder_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = {
            executor.submit(
                PDFProcessor.process_pdf,
                file_path,
                progress_callback,
            ): file_path
            for file_path in file_list
        }

        for future in concurrent.futures.as_completed(futures):
            file_path = futures[future]
            try:
                success, message = await future
                if success:
                    file_tracker.mark_purged(file_path)
                    progress_manager.add_message(message, "success")
                else:
                    file_tracker.mark_skipped(file_path)
                    progress_manager.add_message(message, "error")

            except Exception as e:
                file_tracker.mark_skipped(file_path)
                progress_manager.add_message(f"Error processing {file_path}: {e}", "error")

            finally:
                progress_manager.increment_processed()
                progress_callback(progress_manager.get_state())

                if process_manager._stop_requested:
                    break

    process_manager.reset()
    return True, "Folder processing completed."
        
class StyleManager:
    """Manage application styles."""
    
    @staticmethod
    def apply_base_styles():
        """Apply base application styles."""
        ui.add_head_html("""
            <style>
                .folder-row {
                    border: 1px solid #e2e8f0;
                    border-radius: 0.5rem;
                    padding: 1rem;
                    margin-bottom: 1rem;
                }
                .directory-label {
                    color: #64748b;
                    font-size: 0.875rem;
                    margin-bottom: 0.25rem;
                }
                .directory-path {
                    background-color: #f8fafc;
                    border: 1px solid #e2e8f0;
                    border-radius: 0.375rem;
                    padding: 0.5rem;
                    font-family: monospace;
                }
                .action-button {
                    min-width: 6rem;
                }
                .progress-container {
                    margin-top: 1rem;
                }
                .messages-log {
                    height: 12rem;
                    border: 1px solid #e2e8f0;
                    border-radius: 0.375rem;
                    padding: 0.5rem;
                    overflow-y: auto;
                    font-family: monospace;
                    font-size: 0.875rem;
                }
            </style>
        """)

class FolderPickerDialog(ui.dialog):
    """Custom folder picker dialog."""
    
    def __init__(self, directory: str = ".", show_hidden: bool = False):
        """Initialize folder picker dialog."""
        super().__init__()
        self.path = Path(directory).expanduser()
        self.show_hidden = show_hidden
        self.drives_toggle = None  # Initialize here
        
        with self, ui.card():
            self.setup_drive_selector()
            self.setup_folder_grid()
            self.setup_buttons()
            self.update_grid()

    def setup_drive_selector(self):
        """Setup drive selection dropdown for Windows."""
        if platform.system() == "Windows":
            import win32api
            drives = win32api.GetLogicalDriveStrings().split('\000')[:-1]
            self.drives_toggle = ui.toggle(drives, value=drives[0]).props("inline")  # Initialize ui.toggle here
            self.drives_toggle.bind_value(self._handle_drive_change)

    def _handle_drive_change(self, value):
            """Update current path when drive is changed."""
            self.path = Path(value)
            self.update_grid()

    def setup_folder_grid(self):
        """Setup the folder grid display."""
        self.grid = ui.aggrid({
            'columnDefs': [
                {'field': 'name', 'headerName': 'Folder'}
            ],
            'rowSelection': 'single',
        }, html_columns=[0]).classes('w-96')
        self.grid.on('cellDoubleClicked', self.handle_double_click)

    def setup_buttons(self):
        """Setup dialog control buttons."""
        with ui.row().classes('w-full justify-end gap-2'):
            ui.button('Cancel', on_click=self.close).props('flat')
            ui.button('Select', on_click=self._handle_select).props('primary')


    def update_grid(self):
        """Update the folder grid display."""
        try:
            paths = list(self.path.glob('*'))
            if not self.show_hidden:
                paths = [p for p in paths if not p.name.startswith('.')]
            paths = [p for p in paths if p.is_dir()]
            paths.sort(key=lambda p: p.name.lower())

            self.grid.options['rowData'] = [
                {
                    'name': f'📁 <strong>{p.name}</strong>',
                    'path': str(p),
                }
                for p in paths
            ]

            # Add parent directory option if not at root
            if self.path != self.path.parent:
                self.grid.options['rowData'].insert(0, {
                    'name': '📁 <strong>..</strong>',
                    'path': str(self.path.parent),
                })

            self.grid.update()
        except Exception as e:
            ui.notify(f'Error updating folder list: {str(e)}', type='error')

    async def handle_double_click(self, e):
        """Handle double-click on folder."""
        path = Path(e.args['data']['path'])
        if path.is_dir():
            self.path = path
            self.update_grid()
        else:
            self.submit([str(path)])

    async def _handle_select(self):
        """Handle folder selection."""
        rows = await self.grid.get_selected_rows()
        if rows:
            self.submit([rows[0]['path']])
        else:
            self.submit([str(self.path)])

class ProgressUI:
    """Manage progress display components."""
    
    def __init__(self):
        self.progress_bar = None
        self.status_label = None
        self.messages_log = None
        self.setup_components()

    def setup_components(self):
        """Setup progress UI components."""
        with ui.column().classes('w-full gap-2 progress-container'):
            self.progress_bar = ui.linear_progress(value=0).props('rounded')
            self.status_label = ui.label().classes('text-sm text-gray-600')
            self.messages_log = ui.log().classes('messages-log')

    def update(self, progress_data: Dict):
        """Update progress display."""
        if 'progress' in progress_data:
            self.progress_bar.set_value(progress_data['progress'])
        
        if 'message' in progress_data:
            message = progress_data['message']
            message_type = progress_data.get('type', 'info')
            
            prefix = {
                'info': 'ℹ️',
                'warning': '⚠️',
                'error': '❌',
                'success': '✅'
            }.get(message_type, '')
            
            formatted_message = f"{prefix} {message}" if prefix else message
            self.messages_log.push(formatted_message)
            
            if message_type != 'progress':
                self.status_label.text = message

    def clear(self):
        """Clear progress display."""
        self.progress_bar.set_value(0)
        self.status_label.text = ""
        self.messages_log.clear()

class FolderRow:
    """Manage folder row UI components."""
    
    def __init__(self, folder_path: str, on_update, on_remove):
        self.folder_path = folder_path
        self.on_update = on_update
        self.on_remove = on_remove
        self.progress_ui = None
        self.container = None
        self.setup_row()

    def setup_row(self):
        """Setup folder row components."""
        self.container = ui.column().classes('folder-row')
        
        with self.container:
            # Directory section
            with ui.column():
                ui.label('Directory:').classes('directory-label')
                with ui.row().classes('w-full items-center gap-2'):
                    display_name = Path(self.folder_path).name or self.folder_path
                    path_label = ui.label(display_name).classes('directory-path flex-grow')
                    
                    async def browse():
                        picker = FolderPickerDialog(self.folder_path)
                        result = await picker
                        if result:
                            self.folder_path = result[0]
                            path_label.text = Path(result[0]).name or result[0]
                            await self.on_update(self)

                    ui.button('Browse', on_click=browse).classes('action-button')

            # Action buttons
            with ui.row().classes('w-full justify-between mt-4'):
                with ui.row().classes('gap-2'):
                    ui.button('Update', on_click=lambda: self.on_update(self)).classes('action-button').props('primary')
                    ui.button('Remove', on_click=lambda: self.on_remove(self)).classes('action-button')

            # Progress section
            self.progress_ui = ProgressUI()

    def update_progress(self, progress_data: Dict):
        """Update progress display."""
        if self.progress_ui:
            self.progress_ui.update(progress_data)

    def clear_progress(self):
        """Clear progress display."""
        if self.progress_ui:
            self.progress_ui.clear()

class ControlPanel:
    """Manage application control panel."""
    
    def __init__(self, on_start_all, on_stop_all, on_reset):
        self.thread_count = None  # Will be initialized in setup_panel
        self.on_start_all = on_start_all
        self.on_stop_all = on_stop_all
        self.on_reset = on_reset
        self.container = None
        self.setup_panel()

    def setup_panel(self):
        """Setup control panel components."""
        self.container = ui.row().classes('w-full justify-between items-center p-4 bg-gray-50 rounded-lg')
        
        with self.container:
            # Action buttons on the left
            with ui.row().classes('gap-4'):
                ui.button('Start All', on_click=self.on_start_all) \
                    .props('primary') \
                    .classes('action-button')
                    
                ui.button('Stop All', on_click=self.on_stop_all) \
                    .props('negative') \
                    .classes('action-button')
                    
                ui.button('Reset', on_click=self.on_reset) \
                    .classes('action-button')

            # Thread control on the right
            with ui.row().classes('gap-2 items-center'):
                ui.label('Threads:')
                self.thread_count = ui.number(value=4, min=1, max=16).props('size=sm')

class Header:
    """Manage application header."""
    
    @staticmethod
    def create():
        """Create header components."""
        with ui.column().classes('w-full text-center mb-6'):
            ui.label('PDF Purger').classes('text-2xl font-bold mb-2')
            ui.label('Removes images and vector graphics while ensuring text visibility') \
                .classes('text-gray-600')
                
class PDFPurgerApp:
    """Main application class for PDF Purger."""
    
    def __init__(self):
        """Initialize application state and managers."""
        self.process_manager = ProcessManager()
        self.folder_rows: List[FolderRow] = []
        self.control_panel: Optional[ControlPanel] = None
        self.current_tasks: List[asyncio.Task] = []
        self._folder_paths: List[str] = load_state()
        
        # Setup page routes
        @ui.page('/')
        async def index_page():
            """Main application page."""
            # Initialize storage when page loads
            if 'processing' not in app.storage.user:
                app.storage.user['processing'] = False
            if 'stop_requested' not in app.storage.user:
                app.storage.user['stop_requested'] = False
            if 'active_folders' not in app.storage.user:
                app.storage.user['active_folders'] = []
            if 'folder_paths' not in app.storage.user:
                app.storage.user['folder_paths'] = self._folder_paths.copy()
            
            # Sync process manager with storage
            self.process_manager.sync_with_storage()
            
            await self.initialize_page()

        @ui.page('/process/{folder_path:path}')
        async def process_page(folder_path: str):
            """Processing status page for a specific folder."""
            # Initialize storage when page loads
            if 'processing' not in app.storage.user:
                app.storage.user['processing'] = False
            if 'stop_requested' not in app.storage.user:
                app.storage.user['stop_requested'] = False
            if 'active_folders' not in app.storage.user:
                app.storage.user['active_folders'] = []
            if 'folder_paths' not in app.storage.user:
                app.storage.user['folder_paths'] = self._folder_paths.copy()
            
            # Sync process manager with storage
            self.process_manager.sync_with_storage()
            
            await self.initialize_process_page(folder_path)

    async def initialize_page(self):
        """Initialize main page UI and components."""
        StyleManager.apply_base_styles()
        
        with ui.column().classes('w-full max-w-4xl mx-auto p-4'):
            # Header
            Header.create()
            
            # Control Panel
            self.control_panel = ControlPanel(
                on_start_all=self.start_all_folders,
                on_stop_all=self.stop_all_processing,
                on_reset=self.reset_state
            )
            
            # Folder Management Section
            with ui.column().classes('w-full gap-4'):
                ui.label('Folder Management').classes('text-xl font-bold mt-6 mb-2')
                
                # Create rows for existing folders
                for folder_path in app.storage.user['folder_paths']:
                    await self.add_folder_row(folder_path)
                
                # Add Folder button
                with ui.row().classes('w-full justify-center mt-4'):
                    async def browse_and_add():
                        picker = FolderPickerDialog()
                        result = await picker
                        if result:
                            await self.add_folder_row(result[0])
                    ui.button('Add Folder', 
                             on_click=browse_and_add,
                             icon='add').classes('w-1/3')

    async def initialize_process_page(self, folder_path: str):
        """Initialize processing status page."""
        StyleManager.apply_base_styles()
        
        with ui.column().classes('w-full max-w-4xl mx-auto p-4'):
            # Header with back button
            with ui.row().classes('w-full items-center mb-4'):
                ui.button(icon='arrow_back', on_click=lambda: ui.navigate.to('/'))
                ui.label(f'Processing: {Path(folder_path).name}').classes('text-xl font-bold ml-4')
            
            # Progress components
            progress_ui = ProgressUI()
            
            # Control buttons
            with ui.row().classes('w-full justify-end gap-2 mb-4'):
                ui.button('Stop', on_click=self.stop_all_processing).classes('bg-red-500')
            
            # Start processing
            try:
                await self.process_folder(folder_path, progress_ui, self.control_panel.thread_count.value if self.control_panel else 4)
            except Exception as e:
                logger.error(f"Error in process page: {e}")
                ui.notify(f'Error: {str(e)}', type='negative')

    async def add_folder_row(self, folder_path: str = ""):
        """Add a new folder row to the UI."""
        row = FolderRow(
            folder_path=folder_path,
            on_update=self.handle_folder_update,
            on_remove=self.handle_folder_remove
        )
        self.folder_rows.append(row)
        
        # Update storage state if folder_path provided
        if folder_path:
            paths = list(app.storage.user['folder_paths'])
            if folder_path not in paths:
                paths.append(folder_path)
                app.storage.user['folder_paths'] = paths
                self._folder_paths = paths.copy()
                save_state(paths)

    async def handle_folder_update(self, folder_row: FolderRow):
        """Handle folder update request."""
        if self.process_manager.is_processing():
            ui.notify('Please stop current processing first', 
                     type='warning')
            return
            
        if not folder_row.folder_path:
            ui.notify('Please select a folder first', 
                     type='warning')
            return
            
        # Update storage state
        paths = list(app.storage.user['folder_paths'])
        
        
        if folder_row.folder_path not in paths:
             paths.append(folder_row.folder_path)
             app.storage.user['folder_paths'] = paths
             self._folder_paths = paths.copy()
             save_state(paths)
        else:
            # Ensure correct index
            index = paths.index(folder_row.folder_path)
            paths[index] = folder_row.folder_path
            app.storage.user['folder_paths'] = paths
            self._folder_paths = paths.copy()
            save_state(paths)
        
        # Clear and setup row again with the correct folder path
        folder_row.container.clear()
        folder_row.setup_row()

    async def handle_folder_remove(self, folder_row: FolderRow):
        """Handle folder removal request."""
        if self.process_manager.is_processing():
            await self.stop_all_processing()
            await asyncio.sleep(0.5)
            
        # Update storage state
        paths = list(app.storage.user['folder_paths'])
        if folder_row.folder_path in paths:
            paths.remove(folder_row.folder_path)
            app.storage.user['folder_paths'] = paths
            self._folder_paths = paths.copy()
            save_state(paths)
            
        # Remove from UI
        if folder_row in self.folder_rows:
            self.folder_rows.remove(folder_row)
            folder_row.container.clear()
            ui.notify(f'Removed folder: {Path(folder_row.folder_path).name}',
                     type='info')

    async def process_folder(self, folder_path: str, progress_ui: ProgressUI, thread_count: int):
        """Process a single folder."""
        try:
            # Create progress callback
            def progress_callback(progress_data: Dict):
                progress_ui.update(progress_data)

            # Start processing
            success, message = await process_pdf_folder(
                folder_path,
                thread_count,
                progress_callback,
                self.process_manager
            )
            
            if success:
                ui.notify(f'Completed processing {Path(folder_path).name}', type='positive')
            else:
                ui.notify(message, type='negative')
                
        except Exception as e:
            logger.error(f"Error processing folder: {e}")
            ui.notify(f'Error: {str(e)}', type='negative')
        finally:
            # Reset state and progress regardless of success or failure
            progress_ui.clear()

    async def start_all_folders(self):
        """Start processing all folders concurrently."""
        paths = app.storage.user['folder_paths']
        if not paths:
            ui.notify('No folders to process', type='warning')
            return
            
        if self.process_manager.is_processing():
             ui.notify('Processing already in progress', type='warning')
             return

        thread_count = self.control_panel.thread_count.value if self.control_panel else 4
        
        # Create tasks for all folders
        self.current_tasks = [
            asyncio.create_task(self.process_folder(folder_path, ProgressUI(), thread_count))
            for folder_path in paths
        ]
        self.process_manager.start_processing()

        # Wait for all tasks to complete and reset state
        await asyncio.gather(*self.current_tasks, return_exceptions=True)
        self.process_manager.reset()

    async def stop_all_processing(self):
        """Stop all current processing."""
        if self.process_manager.stop_processing():
            ui.notify('Processing stopped', type='warning')
        await asyncio.sleep(0.5)
        ui.navigate.to('/')

    async def reset_state(self):
        """Reset application state."""
        await self.stop_all_processing()
        app.storage.user['folder_paths'] = []
        self._folder_paths = []
        save_state([])
        ui.notify('Application state reset', type='info')
        ui.navigate.reload()

def main():
    """Main application entry point."""
    # Generate random secret for storage
    import os
    storage_secret = os.urandom(16).hex()
    
    try:
        # Initialize application
        app = PDFPurgerApp()
        
        logger.info("Application initialized successfully")
        
        # Run the application
        ui.run(
            storage_secret=storage_secret,
            title="PDF Purger",
            favicon="📄",
            dark=False,
            reload=False,
            port=8080,
        )
        
    except Exception as e:
        logger.error(f"Error initializing application: {e}")
        raise

if __name__ == "__main__":
    main()
