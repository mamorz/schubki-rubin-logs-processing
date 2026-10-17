-- Load the fictional component -> team mapping (data/ownership.csv, see README).

INSERT INTO ownership
SELECT dataset, component, team, area
FROM read_csv('/data/ownership.csv', header = true,
              columns = {'dataset': 'VARCHAR', 'component': 'VARCHAR', 'team': 'VARCHAR', 'area': 'VARCHAR'});
