#!/usr/bin/python3

import sys

sys.path.append("/pash/compiler")

from parse import parse_shell, from_ir_to_shell, from_ir_to_shell_file
from json_ast import parse_json_ast_string, serialize_asts_to_json, json_to_shell
from ast2json import to_string


if (len (sys.argv) != 2):
    print ("Usage: rt.py shell.sh");
    exit (1);

inputFile = sys.argv [1];

# json_ast_string = parse_shell (...) 
# json = parse_json_ast (inputFile);
# from_ir_to_shell

json = parse_shell (inputFile);
#print ("JSON: %s" % json);

asts = parse_json_ast_string (json);
#print (asts);
#print ();

#print ("TODO: directly convert AST to shell script\n");

json_rt = serialize_asts_to_json (asts)
#print ("JSON round-trip: %s" % json_rt);
#print ();


shell_rt = json_to_shell (json_rt);
#print ("Shell round-trip: %s" % shell_rt);

#print ("to_string");
for ast in asts:
    shell_direct = to_string (ast);
    print ("%s" % shell_direct);