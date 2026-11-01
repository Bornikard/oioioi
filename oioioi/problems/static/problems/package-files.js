// The tree of files of a problem package on the "Manage package files" tab.
// Changes are only collected here and sent together in a single reupload.
// Started on DOMContentLoaded, because gettext is loaded at the end of the page.
document.addEventListener("DOMContentLoaded", function () {
    "use strict";

    const container = document.getElementById("package-files");
    const sizes = JSON.parse(document.getElementById("package-file-sizes").textContent);
    const tree = document.getElementById("package-files-tree");
    const canEdit = container.dataset.canEdit === "true";
    const downloadUrl = container.dataset.downloadUrl;
    const maxFiles = Number(container.dataset.maxFiles);
    const MAX_TEXT_SIZE = 1024 * 1024; // Larger files cannot be opened in the editor.

    const form = document.getElementById("package-files-form");
    const picker = document.getElementById("package-files-picker");
    const changesList = document.getElementById("package-changes-list");
    const submitButton = document.getElementById("package-changes-submit");
    const discardButton = document.getElementById("package-changes-discard");

    const editor = document.getElementById("package-file-editor");
    const editorPath = document.getElementById("package-file-editor-path");
    const editorMessage = document.getElementById("package-file-editor-message");
    const editorText = document.getElementById("package-file-editor-text");
    const editorApply = document.getElementById("package-file-editor-apply");

    const originalPaths = new Set(Object.keys(sizes));
    const changes = new Map(); // path -> {op: "replace"|"add"|"delete", file}
    const newDirs = new Set(); // Folders created here, kept in the tree even when empty.
    const openDirs = new Set(); // Folders the user has expanded.
    let query = "";
    let submitting = false;
    let dropHighlight = null;
    let editedPath = null;
    let editedFileUsesCrlf = false;
    let loadedText = "";
    let editorRequest = 0; // Lets a slow load notice that the editor has moved on.

    const rootDir = originalPaths.values().next().value.split("/")[0];
    openDirs.add(rootDir);

    function formatSize(bytes) {
        if (bytes < 1024) {
            return bytes + " B";
        }
        if (bytes < 1024 * 1024) {
            return (bytes / 1024).toFixed(1) + " KB";
        }
        return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }

    function basename(path) {
        return path.slice(path.lastIndexOf("/") + 1);
    }

    function element(tag, className, text) {
        const result = document.createElement(tag);
        result.className = className;
        if (text !== undefined) {
            result.textContent = text;
        }
        return result;
    }

    function iconButton(icon, label, onClick) {
        const button = element("button", "btn btn-sm btn-outline-secondary");
        button.type = "button";
        button.title = label;
        button.setAttribute("aria-label", label);
        button.appendChild(element("i", icon));
        button.addEventListener("click", function (event) {
            // Buttons in a folder's <summary> must not toggle the folder.
            event.preventDefault();
            onClick();
        });
        return button;
    }

    function pickFiles(multiple, onPicked) {
        picker.multiple = multiple;
        picker.value = "";
        picker.onchange = function () {
            onPicked(Array.from(picker.files));
        };
        picker.click();
    }

    function isDownloadable(path) {
        // Other file names are rejected by the download URL.
        return originalPaths.has(path) && /^[0-9a-zA-Z\-_=./]+$/.test(path);
    }

    function fileUrl(path) {
        return downloadUrl.replace("PATH", path);
    }

    function canOpenInEditor(path) {
        const change = changes.get(path);
        if (change && change.file) {
            return change.file.size <= MAX_TEXT_SIZE;
        }
        return isDownloadable(path) && sizes[path] <= MAX_TEXT_SIZE;
    }

    async function readFileBytes(path) {
        const change = changes.get(path);
        if (change && change.file) {
            return change.file.arrayBuffer();
        }
        const response = await fetch(fileUrl(path));
        if (!response.ok) {
            throw new Error(response.statusText);
        }
        return response.arrayBuffer();
    }

    function decodeText(bytes) {
        // Strict decoding, so that saving can never damage a file in another encoding.
        try {
            const text = new TextDecoder("utf-8", {fatal: true, ignoreBOM: true}).decode(bytes);
            return text.includes("\0") ? null : text;
        } catch {
            return null;
        }
    }

    function showEditorMessage(message) {
        editorMessage.textContent = message;
        editorMessage.hidden = !message;
    }

    function isEditorDirty() {
        return !editorText.readOnly && !editorText.disabled && editorText.value !== loadedText;
    }

    async function openEditor(path) {
        const request = ++editorRequest;
        const change = changes.get(path);
        editedPath = path;
        editorPath.textContent = path.slice(rootDir.length + 1);
        editorText.value = "";
        editorText.disabled = true;
        editorText.readOnly = !canEdit || Boolean(change && change.op === "delete");
        editorText.hidden = false;
        editorApply.hidden = editorText.readOnly;
        editorApply.disabled = true;
        showEditorMessage(gettext("Loading..."));
        editor.showModal();

        let bytes;
        try {
            bytes = await readFileBytes(path);
        } catch {
            if (request === editorRequest) {
                editorText.hidden = true;
                showEditorMessage(gettext("Could not load this file."));
            }
            return;
        }
        if (request !== editorRequest) {
            return;
        }
        const text = decodeText(bytes);
        if (text === null) {
            editorText.hidden = true;
            showEditorMessage(gettext("This file is not UTF-8 text, so it cannot be shown here. Download it instead."));
            return;
        }
        editedFileUsesCrlf = text.includes("\r\n");
        editorText.value = text;
        loadedText = editorText.value; // A textarea normalizes line endings to \n.
        editorText.disabled = false;
        showEditorMessage("");
    }

    function closeEditor() {
        if (isEditorDirty() && !window.confirm(gettext("Discard your edits to this file?"))) {
            return;
        }
        editorRequest++;
        editorText.value = "";
        loadedText = "";
        editor.close();
    }

    function applyEditor() {
        const content = editedFileUsesCrlf ? editorText.value.replace(/\n/g, "\r\n") : editorText.value;
        setFileContent(editedPath, new File([content], basename(editedPath)));
        loadedText = editorText.value;
        closeEditor();
        render();
    }

    function highlightDropTarget(target) {
        if (target !== dropHighlight) {
            dropHighlight?.classList.remove("bg-primary-subtle");
            target?.classList.add("bg-primary-subtle");
            dropHighlight = target;
        }
    }

    function isFileDrag(event) {
        return event.dataTransfer.types.includes("Files");
    }

    function droppedFiles(event) {
        const items = Array.from(event.dataTransfer.items);
        if (items.some((item) => item.webkitGetAsEntry()?.isDirectory)) {
            window.alert(gettext("Folders cannot be dropped here, drop the files inside them instead."));
            return [];
        }
        return Array.from(event.dataTransfer.files);
    }

    // Nested targets stop the events, so the innermost one under the cursor wins.
    function makeDropTarget(target, onDrop) {
        if (!canEdit) {
            return;
        }
        target.addEventListener("dragover", function (event) {
            if (isFileDrag(event)) {
                event.preventDefault();
                event.stopPropagation();
                event.dataTransfer.dropEffect = "copy";
                highlightDropTarget(target);
            }
        });
        target.addEventListener("drop", function (event) {
            if (isFileDrag(event)) {
                event.preventDefault();
                event.stopPropagation();
                highlightDropTarget(null);
                const files = droppedFiles(event);
                if (files.length) {
                    onDrop(files);
                }
            }
        });
    }

    function allDirs() {
        const dirs = new Set(newDirs);
        for (const path of [...originalPaths, ...changes.keys()]) {
            for (let i = path.indexOf("/"); i !== -1; i = path.indexOf("/", i + 1)) {
                dirs.add(path.slice(0, i));
            }
        }
        return dirs;
    }

    function setFileContent(path, file) {
        changes.set(path, {op: originalPaths.has(path) ? "replace" : "add", file: file});
    }

    function addFiles(dir, files) {
        const dirs = allDirs();
        const conflicting = files.filter((file) => dirs.has(dir + "/" + file.name));
        if (conflicting.length) {
            window.alert(gettext("A folder with this name already exists: ") + conflicting[0].name);
            return;
        }
        for (const file of files) {
            setFileContent(dir + "/" + file.name, file);
        }
        openDirs.add(dir);
        render();
    }

    function createFolder(parent) {
        const name = (window.prompt(gettext("Folder name:")) || "").trim();
        if (!name) {
            return;
        }
        const path = parent + "/" + name;
        if (/[/\\]/.test(name) || name === "." || name === "..") {
            window.alert(gettext("A folder name cannot contain slashes."));
        } else if (allDirs().has(path) || originalPaths.has(path) || changes.has(path)) {
            window.alert(gettext("This name is already used in this folder."));
        } else {
            newDirs.add(path);
            openDirs.add(parent);
            openDirs.add(path);
            render();
        }
    }

    function removeFile(path) {
        if (originalPaths.has(path)) {
            changes.set(path, {op: "delete"});
        } else {
            changes.delete(path);
        }
        render();
    }

    function undoChange(path) {
        changes.delete(path);
        render();
    }

    function buildTree() {
        const top = {dirs: new Map(), files: []};
        function dirNode(path) {
            let node = top;
            let prefix = "";
            for (const part of path.split("/")) {
                prefix = prefix ? prefix + "/" + part : part;
                if (!node.dirs.has(part)) {
                    node.dirs.set(part, {name: part, path: prefix, dirs: new Map(), files: []});
                }
                node = node.dirs.get(part);
            }
            return node;
        }
        newDirs.forEach(dirNode);
        for (const path of new Set([...originalPaths, ...changes.keys()])) {
            dirNode(path.slice(0, path.lastIndexOf("/"))).files.push(path);
        }
        return top;
    }

    function fileRow(path) {
        const change = changes.get(path);
        const row = element("div", "d-flex align-items-center gap-2 py-1 border-bottom");
        row.appendChild(element("i", "fa-regular fa-file text-muted"));

        const textClass = {replace: "text-warning-emphasis fw-bold", add: "text-success fw-bold", delete: "text-decoration-line-through text-muted"};
        row.appendChild(element("span", "text-break " + (change ? textClass[change.op] : ""), basename(path)));

        const size = change && change.file ? change.file.size : sizes[path];
        const spacer = element("span", "ms-auto text-muted small text-nowrap");
        if (size !== undefined) {
            spacer.textContent = formatSize(size);
        }
        row.appendChild(spacer);

        const actions = element("div", "d-flex gap-1 flex-shrink-0");
        if (canOpenInEditor(path)) {
            const editable = canEdit && (!change || change.op !== "delete");
            actions.appendChild(iconButton("fa-regular fa-eye", editable ? gettext("View or edit") : gettext("View"), () => openEditor(path)));
        }
        if (isDownloadable(path)) {
            const link = element("a", "btn btn-sm btn-outline-secondary");
            link.href = fileUrl(path);
            link.download = basename(path);
            link.title = gettext("Download");
            link.setAttribute("aria-label", gettext("Download"));
            link.appendChild(element("i", "fa-solid fa-download"));
            actions.appendChild(link);
        }
        if (canEdit) {
            if (!change || change.op !== "delete") {
                actions.appendChild(
                    iconButton("fa-solid fa-arrows-rotate", gettext("Replace"), () =>
                        pickFiles(false, (files) => {
                            setFileContent(path, files[0]);
                            render();
                        })
                    )
                );
            }
            if (change && change.op !== "add") {
                actions.appendChild(iconButton("fa-solid fa-rotate-left", gettext("Undo"), () => undoChange(path)));
            }
            if (!change || change.op !== "delete") {
                actions.appendChild(iconButton("fa-solid fa-trash", gettext("Delete"), () => removeFile(path)));
            }
        }
        row.appendChild(actions);
        makeDropTarget(row, function (files) {
            if (files.length === 1) {
                setFileContent(path, files[0]);
                render();
            } else {
                addFiles(path.slice(0, path.lastIndexOf("/")), files);
            }
        });
        return row;
    }

    function dirElement(node) {
        const content = element("div", "ps-3");
        const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
        for (const dir of dirs) {
            const child = dirElement(dir);
            if (child) {
                content.appendChild(child);
            }
        }
        const files = node.files.filter((path) => path.toLowerCase().includes(query)).sort((a, b) => a.localeCompare(b));
        files.forEach((path) => content.appendChild(fileRow(path)));
        if (query && !content.childElementCount) {
            return null;
        }

        const details = element("details", "");
        details.open = Boolean(query) || openDirs.has(node.path);
        details.addEventListener("toggle", function () {
            if (!query) {
                if (details.open) {
                    openDirs.add(node.path);
                } else {
                    openDirs.delete(node.path);
                }
            }
        });

        const summary = element("summary", "d-flex align-items-center gap-2 py-1 border-bottom");
        summary.appendChild(element("i", "fa-solid fa-folder text-warning"));
        summary.appendChild(element("span", "fw-bold text-break", node.name));
        const total = countFiles(node);
        const count = element("span", "ms-auto text-muted small text-nowrap");
        count.textContent = interpolate(ngettext("%s file", "%s files", total), [total]);
        summary.appendChild(count);

        if (canEdit) {
            const actions = element("div", "d-flex gap-1 flex-shrink-0");
            actions.appendChild(iconButton("fa-solid fa-file-circle-plus", gettext("Add files"), () => pickFiles(true, (files) => addFiles(node.path, files))));
            actions.appendChild(iconButton("fa-solid fa-folder-plus", gettext("New folder"), () => createFolder(node.path)));
            summary.appendChild(actions);
        }
        details.appendChild(summary);
        details.appendChild(content);
        makeDropTarget(details, (files) => addFiles(node.path, files));
        return details;
    }

    function countFiles(node) {
        let total = node.files.length;
        node.dirs.forEach((dir) => {
            total += countFiles(dir);
        });
        return total;
    }

    function changeItem(path, change) {
        const item = element("li", "d-flex align-items-center gap-2 py-1 border-bottom");
        const symbol = {replace: "~", add: "+", delete: "−"}[change.op];
        const classes = {replace: "text-warning-emphasis", add: "text-success", delete: "text-danger"};
        item.appendChild(element("span", "fw-bold " + classes[change.op], symbol));
        let label = path.slice(rootDir.length + 1);
        if (change.file && change.file.name !== basename(path)) {
            label += " ← " + change.file.name;
        }
        item.appendChild(element("span", "text-break", label));
        const undo = iconButton("fa-solid fa-rotate-left", gettext("Undo"), () => undoChange(path));
        undo.classList.add("ms-auto");
        item.appendChild(undo);
        return item;
    }

    function renderChanges() {
        changesList.replaceChildren();
        for (const path of [...changes.keys()].sort((a, b) => a.localeCompare(b))) {
            changesList.appendChild(changeItem(path, changes.get(path)));
        }
        document.getElementById("package-changes-count").textContent = changes.size;
        document.getElementById("package-changes-empty").hidden = changes.size > 0;
        submitButton.disabled = changes.size === 0;
        discardButton.disabled = changes.size === 0 && newDirs.size === 0;
    }

    function render() {
        tree.replaceChildren();
        const top = buildTree();
        for (const node of top.dirs.values()) {
            const rendered = dirElement(node);
            if (rendered) {
                tree.appendChild(rendered);
            }
        }
        if (query && !tree.childElementCount) {
            tree.appendChild(element("p", "text-muted", gettext("No matching files.")));
        }
        if (canEdit) {
            renderChanges();
        }
    }

    document.getElementById("package-files-search").addEventListener("input", function (event) {
        query = event.target.value.trim().toLowerCase();
        render();
    });

    editorText.addEventListener("input", function () {
        editorApply.disabled = !isEditorDirty();
    });
    editorApply.addEventListener("click", applyEditor);
    document.querySelectorAll("[data-editor-close]").forEach((button) => button.addEventListener("click", closeEditor));
    editor.addEventListener("cancel", function (event) {
        // Escape goes through the same check for unsaved edits.
        event.preventDefault();
        closeEditor();
    });

    if (canEdit) {
        // A file dropped next to the tree must not make the browser open it and lose the changes.
        window.addEventListener("dragover", function (event) {
            if (isFileDrag(event)) {
                event.preventDefault();
                event.dataTransfer.dropEffect = "none";
                highlightDropTarget(null);
            }
        });
        window.addEventListener("dragleave", function (event) {
            if (!event.relatedTarget) {
                highlightDropTarget(null);
            }
        });
        window.addEventListener("drop", function (event) {
            if (isFileDrag(event)) {
                event.preventDefault();
                highlightDropTarget(null);
            }
        });

        discardButton.addEventListener("click", function () {
            changes.clear();
            newDirs.clear();
            render();
        });

        form.addEventListener("submit", function (event) {
            const paths = [...changes.keys()];
            const uploads = paths.filter((path) => changes.get(path).file);
            if (uploads.length > maxFiles) {
                event.preventDefault();
                window.alert(interpolate(gettext("You can upload at most %s files at once."), [maxFiles]));
                return;
            }
            form.elements.changes.value = JSON.stringify(paths.map((path) => ({op: changes.get(path).op, path: path})));
            paths.forEach(function (path, index) {
                const file = changes.get(path).file;
                if (file) {
                    const transfer = new DataTransfer();
                    transfer.items.add(file);
                    const input = element("input", "d-none");
                    input.type = "file";
                    input.name = "file_" + index;
                    input.files = transfer.files;
                    form.appendChild(input);
                }
            });
            submitting = true;
            submitButton.disabled = true;
        });

        window.addEventListener("beforeunload", function (event) {
            if (changes.size && !submitting) {
                event.preventDefault();
            }
        });
    }

    render();
});
