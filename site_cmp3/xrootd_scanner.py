from pythreader import TaskQueue, Task, DEQueue, PyThread, synchronized
import re, json, os, os.path, traceback
import subprocess, time
from part import PartitionedList
from py3 import to_str
from stats import write_stats

Version = "1.1"

try:
    import tqdm
    Use_tqdm = True
except:
    Use_tqdm = False

from config import Config

def truncated_path(root, path):
        if path == root:
            return "/"
        relpath = path
        if path.startswith(root+"/"):
            relpath = path[len(root)+1:]
        N = 5
        parts = relpath.split("/")
        while parts and not parts[0]:
            parts = parts[1:]
        if len(parts) <= N:
            #return "%s -> %s" % (path, relpath)
            return relpath
        else:
            n = len(parts)
            #return ("%s -> ..(%d)../" % (path, n-N))+"/".join(parts[-N:])
            return ("..(%d)../" % (n-N))+"/".join(parts[-N:])
        

def runCommand(cmd, timeout=None, debug=None):
    return p.returncode, out

class Killer(PyThread):

    def __init__(self, scanner, timeout):
        PyThread.__init__(self)
        self.Scanner = scanner
        self.Timeout = timeout
        self.Stop = False

    def stop(self):
        self.Stop = True

    def run(self):
        t1 = time.time() + self.Timeout
        while not self.Stop and time.time() < t1:
                time.sleep(0.5)
        if not self.Stop:
                self.Scanner.killme()
                self.Scanner = None
    
class Scanner(Task):
    
    MAX_ATTEMPTS_REC = 2
    MAX_ATTEMPTS_FLAT = 5

    def __init__(self, master, server, location, recursive, timeout):
        Task.__init__(self)
        self.Server = server
        self.Master = master
        self.Location = location
        self.Timeout = timeout
        self.WasRecursive = self.Recursive = recursive
        self.Subprocess = None
        self.Done = False
        self.Killed = False
        self.Started = None
        self.Elapsed = None
        self.RecAttempts = self.MAX_ATTEMPTS_REC if recursive else 0
        self.FlatAttempts = self.MAX_ATTEMPTS_FLAT

    def __str__(self):
        return "Scanner(%s)" % (self.Location,)

    def message(self, status, stats):
        if self.Master is not None:
            self.Master.message("%-100s\t%s %s" % (truncated_path(self.Master.Root, self.Location), status, stats))
        

    @synchronized
    def killme(self):
        if self.Subprocess is not None:
                self.Killed = True
                self.Subprocess.terminate()

    def scan(self, recursive):
        server = self.Server
        location = self.Location
        timeout = self.Timeout
        lscommand = "xrdfs %s ls %s %s" % (server, "-R" if recursive else "", location)
        #print("lscommand:", lscommand)

        self.Killed = False
        killer = Killer(self, timeout)

        with self:
                # the killer process will wait for self.Subprocess to become not None or Done to become True
                self.Subprocess = subprocess.Popen(lscommand, shell=True, 
                        stderr=subprocess.PIPE,
                        stdout=subprocess.PIPE)
                killer.start()          # do not start killer until self.Subprocess is set

        out, err = self.Subprocess.communicate()
        out = to_str(out)
        err = to_str(err)

        with self:
                # make this a critical section so the killer process does not intercept us
                killer.stop()
                retcode = self.Subprocess.returncode
                self.Subprocess = None
                
        files = []
        dirs = []
        status = "OK"
        reason = ""
        if self.Killed:
            status = "timeout"
        elif retcode:
            status = "failed"
            reason = "status: %d" % (retcode,)
            if err: reason += " " + err.strip()
        else:
            lines = [x.strip() for x in out.split("\n")]
            for l in lines:
                if l:
                    if not l.startswith(location):
                        status = "failed"
                        reason = "Invalid line in output: %s" % (l,)
                        break
                    last_word = l.rsplit("/",1)[-1]
                    if '.' in last_word:
                        path = l
                        path = path if path.startswith(location) else location + "/" + path
                        if not path.endswith("/."):
                                files.append(path)
                    else:
                        path = l
                        path = path if path.startswith(location) else location + "/" + path
                        dirs.append(path)
        #if not recursive:
        #    print("scan(%s): %d dirs" % (location, len(dirs)))
        return status, reason, dirs, files

    def run(self):
        #print("Scanner.run():", self.Master)
        self.Started = t0 = time.time()
        location = self.Location
        if self.RecAttempts > 0:
            recursive = True
            self.RecAttempts -= 1
        else:
            recursive = False
            self.FlatAttempts -= 1
        self.WasRecursive = recursive
        stats = "r" if recursive else " "
        #self.message("start", stats)

        status, reason, dirs, files = self.scan(recursive)
        self.Elapsed = time.time() - self.Started
        #stats = "%1s %7.3fs" % ("r" if recursive else " ", self.Elapsed)
        stats = "r" if recursive else " "
        
        if status != "OK":
            stats += " " + reason
            self.message(status, stats)
            if self.Master is not None:
                self.Master.scanner_failed(self)

        else:
            counts = " %8d %-8d" % (len(files), len(dirs))
            self.message("done", stats+counts)
            if self.Master is not None:
                self.Master.scanner_succeeded(location, recursive, files, dirs)
        
                
    @staticmethod
    def scan_location(server, location, recursive, timeout):
        s = Scanner(None, server, location, recursive, timeout)
        return s.scan(recursive)
        
    @staticmethod
    def location_exists(server, location, timeout):
        s = Scanner(None, server, location, False, timeout)
        status, reason, dirs, files = s.scan(False)
        return status == "OK", reason
                
class ScannerMaster(PyThread):
    
    MAX_RECURSION_FAILED_COUNT = 5
    REPORT_INTERVAL = 10.0
    
    def __init__(self, server, root, recursive_threshold, max_scanners, timeout, quiet, display_progress, max_files = None):
        PyThread.__init__(self)
        self.RecursiveThreshold = recursive_threshold
        self.Server = server
        self.Root = self.canonic(root)
        self.MaxScanners = max_scanners
        self.Results = DEQueue()
        self.ScannerQueue = TaskQueue(max_scanners)
        self.Timeout = timeout
        self.Done = False
        self.Error = None
        self.Failed = False
        self.RootFailed = False
        self.Directories = set()
        self.RecursiveFailed = {}       # parent path -> count
        self.Errors = {}                # location -> count
        self.GaveUp = set()
        self.LastReport = time.time()
        self.EmptyDirs = set()
        self.NScanned = 0
        self.NToScan = 1 
        self.Quiet = quiet
        self.DisplayProgress = display_progress and Use_tqdm and not quiet
        if self.DisplayProgress:
            self.TQ = tqdm.tqdm(total=self.NToScan, unit="dir")
            self.LastV = 0
        self.NFiles = 0
        self.MaxFiles = max_files       # will stop after number of files found exceeds this number. Used for debugging

    def run(self):
        #
        # scan Root non-recursovely first, if failed, return immediarely
        #
        #server, location, recursive, timeout
        scanner_task = Scanner(self, self.Server, self.Root, self.RecursiveThreshold == 0, self.Timeout)
        self.ScannerQueue.addTask(scanner_task)
        
        """
        status, reason, dirs, files = Scanner.scan_location(self.Server, self.Root, self.RecursiveThreshold == 0, self.Timeout)
        if status != "OK":
            self.Failed = self.RootFailed = True
            self.Error = f"Root scan failed: {reason}"
        else:
            self.scanner_succeeded(self.Root, False, files, dirs)    # this will start recursive scanning
        """    
        self.ScannerQueue.waitUntilEmpty()
        self.Results.close()
        self.Done = True
        self.ScannerQueue.Delegate = None       # detach for garbage collection
        self.ScannerQueue = None
        
    def addFiles(self, files):
        if not self.Failed:
            self.Results.append(files)
            self.NFiles += len(files)

    def canonic(self, path):
        while path and "//" in path:
                path = path.replace("//", "/")
        return path
        
    def parent(self, path):
        parts = path.rsplit("/", 1)
        if len(parts) < 2:
            return "/"
        else:
            return parts[0]
            
    def addDirectory(self, path, scan):
        if not self.Failed:
            path = self.canonic(path)
            self.Directories.add(path)
            if scan:
                assert path.startswith(self.Root)
                relpath = path[len(self.Root):]
                while relpath and relpath[0] == '/':
                    relpath = relpath[1:]
                while relpath and relpath[-1] == '/':
                    relpath = relpath[:-1]
                reldepth = 0 if not relpath else len(relpath.split('/'))
                
                parent = self.parent(path)
                
                recursive = (self.RecursiveThreshold is not None 
                    and reldepth >= self.RecursiveThreshold 
                )
                #if use_recursive:
                #    print("Use recursive for %s" % (path,))

                if self.MaxFiles is None or self.NFiles < self.MaxFiles:
                    self.ScannerQueue.addTask(
                        Scanner(self, self.Server, path, recursive, self.Timeout)
                    )
                    self.NToScan += 1
        
    def addDirectories(self, dirs, scan=True):
        for d in dirs:
            self.addDirectory(d, scan)
        self.show_progress()
        self.report()
            
    @synchronized
    def report(self):
        if time.time() > self.LastReport + self.REPORT_INTERVAL:
            waiting, active = self.ScannerQueue.tasks()
            #sys.stderr.write("--- Locations to scan: %d\n" % (len(active)+len(waiting),))
            self.LastReport = time.time()

    @synchronized
    def scanner_failed(self, scanner):
        path = scanner.Location
        if scanner.WasRecursive:
            with self:
                parent = self.parent(path)
                nfailed = self.RecursiveFailed.get(parent, 0)
                self.RecursiveFailed[parent] = nfailed + 1
                
        if not scanner.WasRecursive:
            with self:
                nerrors = self.Errors.setdefault(path, 0)
                nerrors = self.Errors[path] = nerrors + 1
                
        retry = (scanner.RecAttempts > 0) or (scanner.FlatAttempts > 0)
        if retry:
            print("resubmitted:", scanner.Location, scanner.RecAttempts, scanner.FlatAttempts)
            self.ScannerQueue.addTask(scanner)
        else:
            self.GaveUp.add(path)
            self.NScanned += 1  
            #sys.stderr.write("Gave up on: %s\n" % (path,))
            self.show_progress()            #"Error scanning %s: %s -- retrying" % (scanner.Location, error))
        
    @synchronized
    def scanner_succeeded(self, location, recursive, files, dirs):
        with self:
            if len(files) == 0 and (recursive or len(dirs) == 0):
                self.EmptyDirs.add(location)
            else:
                if location in self.EmptyDirs:
                    self.EmptyDirs.remove(location)
            if recursive:
                parent = self.parent(location)
                nfailed = self.RecursiveFailed.get(parent, 0)
                self.RecursiveFailed[parent] = nfailed - 1      
        self.NScanned += 1
        if files:
            self.addFiles(files)
        if dirs:
            self.addDirectories(dirs, not recursive)
        self.show_progress()

    def files(self):
        while not (self.Done and len(self.Results) == 0):
            lst = self.Results.pop()
            if lst:
                    for path in lst:
                        yield self.canonic(path)
                        
    @synchronized
    def show_progress(self, message=None):
        if self.DisplayProgress:
            self.TQ.total = self.NToScan
            delta = max(0, self.NScanned - self.LastV)
            self.TQ.update(delta)
            self.LastV = self.NScanned
            enf = 0
            if self.NScanned > 0:
                enf = int(self.NFiles * self.NToScan/self.NScanned)
            self.TQ.set_postfix(f=self.NFiles, ed=len(self.EmptyDirs), d=len(self.Directories), enf=enf)
            if message:
                self.TQ.write(message)   
                
    @synchronized
    def message(self, message):
        if not self.Quiet:
                if self.DisplayProgress:
                    self.TQ.write(message)
                else:
                    print(message)
                    sys.stdout.flush()

    def close_progress(self):
        if self.DisplayProgress:
            self.TQ.close()
                
             
            
Usage = """
python xrootd_scanner.py [options] <rse>
    Options:
    -c <config.yaml>            - config file, required
    -o <output file prefix>     - output will be sent to <output>.00000, <output>.00001, ...
    -t <timeout>                - xrdfs ls operation timeout (default 30 seconds)
    -m <max workers>            - default 5
    -R <recursion depth>        - start using -R at or below this depth (dfault 3)
    -n <nparts>
    -d                          - display progress
    -q                          - quiet - only print summary
    -M <max_files>              - stop scanning the root after so many files were found
    -s <stats_file>              - write final statistics to JSON file
"""

def rewrite_path(path, path_prefix, remove_prefix, add_prefix, rewrite_path, rewrite_out):
    
    assert path.startswith(path_prefix)

    path = "/" + path[len(path_prefix):]

    if remove_prefix and path.startswith(remove_prefix):
        path = path[len(remove_prefix):]

    if add_prefix:
        path = add_prefix + path

    if path_filter:
        if not path_filter.search(path):
            continue

    if rewrite_path is not None:
        if not rewrite_path.search(path):
            sys.stderr.write(f"Path rewrite pattern for root {root} did not find a match in path {path}\n")
            sys.exit(1)
        path = rewrite_path.sub(rewrite_out, path)   
    return path
    

def scan_root(rse, root, config, my_stats, stats_file, stats_key, override_recursive_threshold, override_max_scanners, file_list, dir_list):
    
    failed = False
    
    timeout = override_timeout or config.scanner_timeout(rse)
    top_path = root if root.startswith("/") else server_root + "/" + root
    recursive_threshold = override_recursive_threshold or config.scanner_recursion_threshold(rse, root)
    max_scanners = override_max_scanners or config.scanner_workers(rse)

    t0 = time.time()
    root_stats = {
       "root": root,
       "start_time":t0,
       "timeout":timeout,
       "recursive_threshold":recursive_threshold,
       "max_scanners":max_scanners
    }

    my_stats["scanning"] = root_stats
    write_stats(my_stats, stats_file, stats_key)

    exists, reason = Scanner.location_exists(server, top_path, timeout)
    t1 = time.time()
    if not exists:
        print("Root %s does not exist: %s" % (top_path, reason))
        root_stats.update({
            "root_failed": True,
            "error": reason,
            "failed_subdirectories": 0,
            "files": 0,
            "directories": 0,
            "empty_directories":0,
            "end_time":t1,
            "elapsed_time": t1-t0
        })
    else:
        remove_prefix = config.scanner_remove_prefix(rse)
        add_prefix = config.scanner_add_prefix(rse)
        path_filter = config.scanner_filter(rse)
        if path_filter is not None:
            path_filter = re.compile(path_filter)
        rewrite_path, rewrite_out = config.scanner_rewrite(rse)
        if rewrite_path is not None:
            assert rewrite_out is not None
            rewrite_path = re.compile(rewrite_path)

        print("Starting scan of %s:%s with:" % (server, top_path))
        print("  Recursive threshold = %d" % (recursive_threshold,))
        print("  Max scanner threads = %d" % max_scanners)
        print("  Timeout              = %s" % timeout)


        master = ScannerMaster(server, top_path, recursive_threshold, max_scanners, timeout, quiet, display_progress,
            max_files = max_files)
        master.start()

        path_prefix = server_root
        if not path_prefix.endswith("/"):
            path_prefix += "/"
        for path in master.files():
            
            path = rewrite_path(path, path_prefix, remove_prefix, add_prefix, rewrite_path, rewrite_out)
            out_list.add(path)             
            
        if dir_list is not None:
            for path in master.Directories:
                dir_list.add(rewrite_path(path)) 

        if display_progress:
            master.close_progress()

        if master.Failed:
            sys.stderr.write("Scanner failed to scan %s: %s\n" % (root, master.Error))
    
        if master.GaveUp:
            sys.stderr.write("Scanner failed to scan the following %d locations:\n" % (len(master.GaveUp),))
            for p in sorted(list(master.GaveUp)):
                sys.stderr.write(p+"\n")

        print("Files:                %d" % (n,))
        print("Directories found:    %d" % (master.NToScan,))
        print("Directories scanned:  %d" % (master.NScanned,))
        print("Directories:          %d" % (len(master.Directories,)))
        print("  empty directories:  %d" % (len(master.EmptyDirs,)))
        print("Failed directories:   %d" % (len(master.GaveUp),))
        t1 = time.time()
        elapsed = int(t1 - t0)
        s = elapsed % 60
        m = elapsed // 60
        print("Elapsed time:         %dm %02ds\n" % (m, s))

        root_stats.update({
            "root_failed": master.RootFailed,
            "error": master.Error,
            "failed_subdirectories": list(master.GaveUp),
            "files": master.NFiles,
            "directories": len(master.Directories),
            "empty_directories":len(master.EmptyDirs),
            "end_time":t1,
            "elapsed_time": t1-t0
        })

        if master.GaveUp:
            failed = True
            
    del my_stats["scanning"]
    my_stats["roots"].append(root_stats)
    write_stats(my_stats, stats_file, stats_key)
    return failed
    


if __name__ == "__main__":
    import getopt, sys, time

    t0 = time.time()    
    opts, args = getopt.getopt(sys.argv[1:], "t:m:o:R:n:c:vqM:s:S:zd:")
    opts = dict(opts)
    
    if len(args) != 1 or not "-c" in opts:
        print(Usage)
        sys.exit(2)

    rse = args[0]
    config = Config(opts.get("-c"))

    quiet = "-q" in opts
    display_progress = not quiet and "-v" in opts
    override_recursive_threshold = int(opts.get("-R", 0))
    override_timeout = int(opts.get("-t", 0))
    override_max_scanners = int(opts.get("-m", 0))
    max_files = opts.get("-M")
    if max_files is not None: max_files = int(max_files)
    stats_file = opts.get("-s")
    stats_key = opts.get("-S", "scanner")
    zout = "-z" in opts
    
    if "-n" in opts:
        nparts = int(opts["-n"])
    else:
        nparts = config.nparts(rse)

    if nparts > 1:
        if not "-o" in opts:
            print ("Output prefix is required for partitioned output")
            print (Usage)
            sys.exit(2)

    output = opts.get("-o","out.list")

    out_list = PartitionedList.create(nparts, output, zout)

    dir_output = opts.get("-d")
    dir_list = PartitionedList.create(nparts, dir_output, zout) if dir_output else None

    server = config.scanner_server(rse)
    server_root = config.scanner_server_root(rse)
    if not server_root:
        print(f"Server root is not defined for {rse}. Should be defined as 'server_root'")
        sys.exit(2)

    my_stats = {
        "rse":rse,
        "scanner":{
            "type":"xrootd",
            "version":Version
        },
        "server_root":server_root,
        "server":server,
        "roots":[], 
        "start_time":time.time(),
        "end_time": None,
        "status":   "started"
    }
    
    write_stats(my_stats, stats_file, stats_key)
    
    failed = False
        
    for root in config.scanner_roots(rse):
        try:
            failed = scan_root(rse, root, config, my_stats, stats_file, stats_key, override_recursive_threshold, override_max_scanners, out_list, dir_list)
        except:
            exc = traceback.format_exc()
            lines = exc.split("\n")
            scanning = my_stats.get("scanning", {"root":root})
            scanning["exception"] = lines
            scanning["exception_time"] = time.time()
            failed = True
    
        if failed:
            break
           
    out_list.close()

    if failed:
        my_stats["status"] = "failed"
    else:
        my_stats["status"] = "done"
        
    my_stats["end_time"] = time.time()
    write_stats(my_stats, stats_file, stats_key)

    if failed:
        sys.exit(1)
    else:
        sys.exit(0)
 
    
        
