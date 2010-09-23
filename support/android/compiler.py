#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Appcelerator Titanium Mobile
#
# Resource to Android Page Compiler
# Handles JS, CSS and HTML files only
#
import os, sys, re, shutil, tempfile, run, codecs, traceback, types
import jspacker, json
from xml.sax.saxutils import escape
from sgmllib import SGMLParser
from csspacker import CSSPacker
from deltafy import Deltafy

ignoreFiles = ['.gitignore', '.cvsignore', '.DS_Store'];
ignoreDirs = ['.git','.svn','_svn', 'CVS'];

# class for extracting javascripts
class ScriptProcessor(SGMLParser):
	def __init__(self):
		SGMLParser.__init__(self)
		self.scripts = []
	
	def unknown_starttag(self, tag, attrs):
		if tag == 'script':
			for attr in attrs:
				if attr[0]=='src':
					self.scripts.append(attr[1])

class Compiler(object):
	
	def __init__(self,tiapp,project_dir,java,classes_dir,root_dir):
		self.tiapp = tiapp
		self.java = java
		self.appname = tiapp.properties['name']
		self.classes_dir = classes_dir
		self.template_dir = os.path.abspath(os.path.dirname(sys._getframe(0).f_code.co_filename))
		self.appid = tiapp.properties['id']
		self.root_dir = root_dir
		self.project_dir = os.path.abspath(os.path.expanduser(project_dir))
		self.modules = []
		self.jar_libraries = []
		
		json_contents = open(os.path.join(self.template_dir,'dependency.json')).read()
		self.depends_map = json.read(json_contents)
		
		# go ahead and slurp in any required modules
		for required in self.depends_map['required']:
			self.add_required_module(required)
			
		self.module_methods = []
		self.js_files = {}
		self.html_scripts = []
		self.compiled_files = []


	def add_required_module(self,name):
		name = name.lower()
		if name in ('buildhash','builddate'): return # ignore these
		if not name in self.modules:
			self.modules.append(name)
			mf = os.path.join(self.template_dir, 'modules', 'titanium-%s.jar' % name)
			if os.path.exists(mf):
				print "[DEBUG] detected module = %s" % name
				self.jar_libraries.append(mf)
			else:
				print "[INFO] unknown module = %s" % name
				
			if self.depends_map['libraries'].has_key(name):
				for lib in self.depends_map['libraries'][name]:
					lf = os.path.join(self.template_dir,lib)
					if os.path.exists(lf):
						if not lf in self.jar_libraries:
							print "[DEBUG] adding required library: %s" % lib
							self.jar_libraries.append(lf) 

			if self.depends_map['dependencies'].has_key(name):
				for depend in self.depends_map['dependencies'][name]:
					self.add_required_module(depend)
				

	def extract_from_namespace(self,name,line):
		modules = [] 
		methods = []
		f = re.findall(r'%s\.(\w+)' % name,line)
		if len(f) > 0:
			for sym in f:
				mm = self.extract_from_namespace("%s.%s" % (name,sym), line)
				for m in mm[0]:
					method_name = "%s.%s" %(sym,m)
					try:
						methods.index(method_name)
					except:
						methods.append(method_name)
				# skip Titanium.version, Titanium.userAgent and Titanium.name since these
				# properties are not modules
				if sym in ('version','userAgent','name','_JSON','include','fireEvent','addEventListener','removeEventListener','buildhash','builddate'):
					continue
				try:
					modules.index(sym)
				except:	
					modules.append(sym)
		return modules,methods
					
	def extract_and_combine_modules(self,name,line):
		modules,methods = self.extract_from_namespace(name,line)
		if len(modules) > 0:
			for m in modules:
				self.add_required_module(m)
			for m in methods:
				try:
					self.module_methods.index(m)
				except:
					self.module_methods.append(m)
			
	def extract_modules(self,out):
		for line in out.split(';'):
			self.extract_and_combine_modules('Titanium',line)
			self.extract_and_combine_modules('Ti',line)
	
	def compile_javascript(self, fullpath):
		js_jar = os.path.join(self.template_dir, 'js.jar')
		resource_relative_path = os.path.relpath(fullpath, self.project_dir)
		
		# chop off '.js'
		js_class_name = resource_relative_path[:-3]
		escape_chars = ['\\', '/', ' ', '.']
		for escape_char in escape_chars:
			js_class_name = js_class_name.replace(escape_char, '_')
		
		jsc_args = [self.java, '-classpath', js_jar, 'org.mozilla.javascript.tools.jsc.Main',
			'-main-method-class', 'org.appcelerator.titanium.TiScriptRunner', '-g',
			'-package', self.appid + '.js', '-o', js_class_name,
			'-d', os.path.join(self.root_dir, 'bin'), fullpath]
			
		print "[DEBUG] compiling javascript: %s" % resource_relative_path
		sys.stdout.flush()
		
		run.run(jsc_args)
		
	def compile_into_bytecode(self, paths):
		compile_js = False
		
		# we only optimize for production deploy type or if it's forcefully overridden with ti.android.compilejs
		if self.tiapp.has_app_property("ti.android.compilejs"):
			if self.tiapp.to_bool(self.tiapp_get_app_property('ti.android.compilejs')):
				print "[DEBUG] Found ti.android.compilejs=true, overriding default (this may take some time)"
				sys.stdout.flush()
				compile_js = True
		elif self.tiapp.has_app_property('ti.deploytype'):
			if self.tiapp.get_app_property('ti.deploytype') == 'production':
				print "[DEBUG] Deploy type is production, turning on JS compilation"
				sys.stdout.flush()
				compile_js = True
		
		if not compile_js: return
		
		for fullpath in paths:
			# skip any JS found inside HTML <script>
			if fullpath in self.html_scripts: continue
			self.compile_javascript(fullpath)
		
	def get_ext(self, path):
		fp = os.path.splitext(path)
		return fp[1][1:]
		
	def make_function_from_file(self, path, pack=True):
		ext = self.get_ext(path)
		path = os.path.expanduser(path)
		file_contents = codecs.open(path,'r',encoding='utf-8').read()
			
		if pack: 
			file_contents = self.pack(path, ext, file_contents)
			
		if ext == 'js':
			# determine which modules this file is using
			self.extract_modules(file_contents)
			
		return file_contents
		
	def pack(self, path, ext, file_contents):
		def jspack(c): return jspacker.jsmin(c)
		def csspack(c): return CSSPacker(c).pack()
		
		packers = {'js': jspack, 'css': csspack }
		if ext in packers:
			file_contents = packers[ext](file_contents)
			of = codecs.open(path,'w',encoding='utf-8')
			of.write(file_contents)
			of.close()
		return file_contents
	
	def extra_source_inclusions(self,path):
		content = codecs.open(path,'r',encoding='utf-8').read()
		p = ScriptProcessor()
		p.feed(content)
		p.close()
		for script in p.scripts:
			# ignore remote scripts
			if script.startswith('http://') or script.startswith('https://'): continue
			# resolve to a full path
			p = os.path.abspath(os.path.join(os.path.join(path,'..'),script))
			self.html_scripts.append(p)
			
	def compile(self):
		print "[INFO] Compiling Javascript resources ..."
		sys.stdout.flush()
		for root, dirs, files in os.walk(self.project_dir):
			for dir in dirs:
				if dir in ignoreDirs:
					dirs.remove(dir)
			if len(files) > 0:
				prefix = root[len(self.project_dir):]
				for f in files:
					fp = os.path.splitext(f)
					if len(fp)!=2: continue
					if fp[1] == '.jss': continue
					if not fp[1] in ['.html','.js','.css']: continue
					if f in ignoreFiles: continue
					fullpath = os.path.join(root,f)
					if fp[1] == '.html':
						self.extra_source_inclusions(fullpath)
					if fp[1] == '.js':
						relative = prefix[1:]
						js_contents = self.make_function_from_file(fullpath, pack=False)
						if relative!='':
							key = "%s_%s" % (relative,f)
						else:
							key = f
						key = key.replace('.js','').replace('\\','_').replace('/','_').replace(' ','_').replace('.','_')
						self.js_files[fullpath] = (key, js_contents)
		self.compile_into_bytecode(self.js_files)
					
					
if __name__ == "__main__":
	if len(sys.argv) != 2:
		print "Usage: %s <projectdir>" % sys.argv[0]
		sys.exit(1)

	project_dir = os.path.expanduser(sys.argv[1])
	resources_dir = os.path.join(project_dir, 'Resources')
	root_dir = os.path.join(project_dir, 'build', 'android')
	destdir = os.path.join(root_dir, 'bin')
	sys.path.append("..")
	import tiapp
	xml = tiapp.TiAppXML(os.path.join(project_dir, 'tiapp.xml'))

	c = Compiler(xml, resources_dir, 'java', destdir, root_dir)
	project_deltafy = Deltafy(resources_dir)
	project_deltas = project_deltafy.scan()
	c.compile()
