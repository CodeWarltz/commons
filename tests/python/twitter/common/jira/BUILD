python_tests(name = 'jira',
  sources = ['test_jira.py'],
  dependencies = [
    pants('src/python/twitter/common/jira'),
    pants('3rdparty/python:mock')
  ]
)

python_test_suite(name = 'all',
  dependencies = [
    pants(':jira'),
  ]
)
